"""Helper methods"""

from __future__ import absolute_import

import json
import logging
import math
import os
import re
import subprocess
import time

import parmap
import psycopg2
import psycopg2.extras
import yaml
from pgcopy import CopyManager
from psycopg2.sql import SQL, Composed, Identifier
from tqdm import trange

from pganonymize.constants import DEFAULT_CHUNK_SIZE, DEFAULT_PRIMARY_KEY
from pganonymize.providers import provider_registry


def branch(tree, path, value):
    key = path[0]
    array_index_match = re.search(r'\[([0-9]+)\]', key)

    if array_index_match:
        # Get the array index, and remove the match from the key
        array_index = int(array_index_match[0].replace('[', '').replace(']', ''))
        key = key.replace(array_index_match[0], '')

        # Prepare the array at the key
        if key not in tree:
            tree[key] = []
        # Prepare the object at the array index
        if array_index == len(tree[key]):
            tree[key].append({})
        # Replace the object at the array index
        tree[key][array_index] = value if len(path) == 1 else branch(tree[key][array_index], path[1:], value)
    else:
        # Prepare the object at the key
        if key not in tree:
            tree[key] = {}
        # Replace the object at the key
        tree[key] = value if len(path) == 1 else branch(tree[key], path[1:], value)

    return tree


def create_dict(attributes):
    d = {}
    for path_str in attributes:
        branch(d, path_str.split('.'), path_str)
    return d


def build_pg_json_object(nested_obj: dict, root_col: str) -> str:
    cols = []
    json_build_object = "json_build_object({})"
    for path in nested_obj:
        if isinstance(nested_obj[path], str):
            path_list = nested_obj[path].split('.')
            tail_path_list = [f"['{item}']" for item in path_list[1:]]
            cols.append(f"'{path_list[-1]}', {root_col}{''.join(tail_path_list)}")
        else:
            cols.append(f"'{path}', {build_pg_json_object(nested_obj[path], root_col)}")
    return json_build_object.format(", ".join(cols))


def anonymize_tables(
    connection, definitions, target_schema, verbose=False, dry_run=False, overwrite_values_in_source_tables=False
):
    """
    Anonymize a list of tables according to the schema definition.

    :param connection: A database connection instance.
    :param list definitions: A list of table definitions from the YAML schema.
    :param str target_schema: Target schema where will be created or replaced anonymized table.
    :param bool verbose: Display logging information and a progress bar.
    :param bool dry_run: Script is runnin in dry-run mode, no commit expected.
    """
    for definition in definitions:
        start_time = time.time()
        table_name = list(definition.keys())[0]
        logging.info('Found table definition "%s"', table_name)
        table_definition = definition[table_name]
        columns = table_definition.get('fields', [])
        excludes = table_definition.get('excludes', [])
        search = table_definition.get('search')
        primary_key = table_definition.get('primary_key', DEFAULT_PRIMARY_KEY)
        total_count = get_table_count(connection, table_name, dry_run)
        chunk_size = table_definition.get('chunk_size', DEFAULT_CHUNK_SIZE)
        build_and_then_import_data(
            connection,
            table_name,
            primary_key,
            columns,
            excludes,
            search,
            total_count,
            chunk_size,
            target_schema,
            verbose=verbose,
            dry_run=dry_run,
            overwrite_values_in_source_tables=overwrite_values_in_source_tables
        )
        end_time = time.time()
        logging.info('{} anonymization took {:.2f}s'.format(table_name, end_time - start_time))


def process_row(row, columns, excludes):
    if row_matches_excludes(row, excludes):
        return None
    else:
        row_column_dict = get_column_values(row, columns)
        if row_column_dict:
            for key, value in row_column_dict.items():
                row[key] = value
        else:
            return None
        return row


def build_and_then_import_data(
    connection,
    table,
    primary_key,
    columns,
    excludes,
    search,
    total_count,
    chunk_size,
    target_schema: str,
    verbose=False,
    dry_run=False,
    overwrite_values_in_source_tables=False
):
    """
    Select all data from a table and return it together with a list of table columns.

    :param connection: A database connection instance.
    :param str table: Name of the table to retrieve the data.
    :param str primary_key: Table primary key
    :param list columns: A list of table fields
    :param list[dict] excludes: A list of exclude definitions.
    :param str search: A SQL WHERE (search_condition) to filter and keep only the searched rows.
    :param int total_count: The amount of rows for the current table
    :param int chunk_size: Number of data rows to fetch with the cursor
    :param str target_schema: Name of the pg schema of target table.
    :param bool verbose: Display logging information and a progress bar.
    :param bool dry_run: Script is running in dry-run mode, no commit expected.
    """
    column_names = get_column_names(columns)
    sql_columns = SQL(', ').join([Identifier(column_name) for column_name in [primary_key] + column_names])
    sql_select = SQL('SELECT {columns} FROM {table}').format(table=Identifier(table), columns=sql_columns)
    if search:
        sql_select = Composed([sql_select, SQL(" WHERE {search_condition}".format(search_condition=search))])
    if dry_run:
        sql_select = Composed([sql_select, SQL(" LIMIT 100")])
        logging.info(sql_select.as_string(connection))
    cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor, name='fetch_large_result')
    cursor.execute(sql_select.as_string(connection))
    temp_table = 'tmp_{table}'.format(table=table)
    create_temporary_table(connection, columns, table, temp_table, primary_key)
    batches = int(math.ceil((1.0 * total_count) / (1.0 * chunk_size)))
    for i in trange(batches, desc="Processing {} batches for {}".format(batches, table), disable=not verbose):
        records = cursor.fetchmany(size=chunk_size)
        if records:
            data = parmap.map(process_row, records, columns, excludes, pm_pbar=verbose, pm_parallel=False)
            import_data(connection, temp_table, [primary_key] + column_names, filter(None, data))
    if overwrite_values_in_source_tables:
        apply_anonymized_data_to_current_table(connection, temp_table, table, primary_key, columns)
    else:
        apply_anonymized_data_to_new_table(connection, target_schema, temp_table, table, primary_key, columns)
    cursor.close()


def apply_anonymized_data_to_current_table(connection, temp_table, source_table, primary_key, definitions):
    logging.info('Applying changes on table {}'.format(source_table))
    cursor = connection.cursor()
    create_index_sql = SQL('CREATE INDEX ON {temp_table} ({primary_key})')
    sql = create_index_sql.format(temp_table=Identifier(temp_table), primary_key=Identifier(primary_key))
    cursor.execute(sql.as_string(connection))

    column_names = get_column_names(definitions)
    columns_identifiers = [SQL('{column} = s.{column}').format(column=Identifier(column)) for column in column_names]
    set_columns = SQL(', ').join(columns_identifiers)
    sql_args = {
        "table": Identifier(source_table),
        "columns": set_columns,
        "source": Identifier(temp_table),
        "primary_key": Identifier(primary_key)
    }
    sql = SQL(
        'UPDATE {table} t '
        'SET {columns} '
        'FROM {source} s '
        'WHERE t.{primary_key} = s.{primary_key}'
    ).format(**sql_args)
    cursor.execute(sql.as_string(connection))
    cursor.close()


def apply_anonymized_data_to_new_table(connection, target_schema, temp_table, source_table, primary_key, definitions):
    logging.info('Applying changes on table {}'.format(source_table))
    cursor = connection.cursor()

    column_names = get_column_names(definitions, True)
    nested_column_names = list(filter(lambda col: '.' in col, column_names))
    column_names = [primary_key] + list(filter(lambda col: '.' not in col, column_names))

    if nested_column_names:
        json_dict_schema = create_dict(nested_column_names)
        root_col = nested_column_names[0].split('.')[0]
        jsonb_object = build_pg_json_object(json_dict_schema[root_col], root_col)
        column_names.append(jsonb_object + f" {root_col}")

    sql_args = {
        "table": Identifier(source_table).as_string(connection),
        "columns": ", ".join(column_names),
        "source": Identifier(temp_table).as_string(connection),
        "target_schema": Identifier(target_schema).as_string(connection)
    }

    sql = ("""
        DROP TABLE IF EXISTS {target_schema}.{table};
        CREATE TABLE {target_schema}.{table} AS (SELECT {columns} FROM {source});
        """).format(**sql_args)
    cursor.execute(sql)
    cursor.close()


def row_matches_excludes(row, excludes=None):
    """
    Check whether a row matches a list of field exclusion patterns.

    :param list row: The data row
    :param list excludes: A list of field exclusion roles, e.g.:

    >>> [
    >>>     {'email': ['\\S.*@example.com', '\\S.*@foobar.com', ]}
    >>> ]

    :return: True or False
    :rtype: bool
    """
    excludes = excludes if excludes else []
    for definition in excludes:
        column = list(definition.keys())[0]
        for exclude in definition.get(column, []):
            pattern = re.compile(exclude, re.IGNORECASE)
            if row[column] is not None and pattern.match(row[column]):
                return True
    return False


def create_temporary_table(connection, definitions, source_table, temp_table, primary_key):
    primary_key = primary_key if primary_key else DEFAULT_PRIMARY_KEY
    column_names = get_column_names(definitions)
    sql_columns = SQL(', ').join([Identifier(column_name) for column_name in [primary_key] + column_names])
    ctas_query = SQL("""CREATE TEMP TABLE {temp_table} AS SELECT {columns}
                    FROM {source_table} WITH NO DATA""")
    cursor = connection.cursor()
    cursor.execute(ctas_query.format(temp_table=Identifier(temp_table),
                                     source_table=Identifier(source_table), columns=sql_columns)
                   .as_string(connection)
                   )
    cursor.close()


def import_data(connection, table_name, column_names, data):
    """
    Import the temporary and anonymized data to a temporary table and write the changes back.
    :param connection: A database connection instance.
    :param str table_name: Name of the table to be populated with data.
    :param list column_names: A list of table fields
    :param list data: The table data.
    """
    mgr = CopyManager(connection, table_name, column_names)
    mgr.copy([[escape_str_replace(val) for col, val in row.items()] for row in data])


def get_connection(pg_args):
    """
    Return a connection to the database.

    :param pg_args:
    :return: A psycopg connection instance
    :rtype: psycopg2.connection
    """
    return psycopg2.connect(**pg_args)


def get_table_count(connection, table, dry_run):
    """
    Return the number of table entries.

    :param connection: A database connection instance
    :param str table: Name of the database table
    :param bool dry_run: Script is running in dry-run mode, no commit expected.
    :return: The number of table entries
    :rtype: int
    """
    if dry_run:
        return 100
    else:
        sql = SQL('SELECT COUNT(*) FROM {table}').format(table=Identifier(table))
        cursor = connection.cursor()
        cursor.execute(sql.as_string(connection))
        total_count = cursor.fetchone()[0]
        cursor.close()
        return total_count


def get_column_values(row, columns):
    """
    Return a dictionary for a single data row, with altered data.

    :param psycopg2.extras.DictRow row: A data row from the current table to be altered
    :param list columns: A list of table columns with their provider rules, e.g.:

    >>> [
    >>>     {'guest_email': {'append': '@localhost', 'provider': 'md5'}}
    >>> ]

    :return: A dictionary with all fields that have to be altered and their value for a single data row, e.g.:
        {'guest_email': '12faf5a9bb6f6f067608dca3027c8fcb@localhost'}
    :rtype: dict
    """
    column_dict = {}
    for definition in columns:
        full_column_name = get_column_name(definition, True)
        column_name = get_column_name(definition, False)
        column_definition = definition[full_column_name]
        provider_config = column_definition.get('provider')
        orig_value = nested_get(row, full_column_name)
        # Skip the current column if there is no value to be altered
        if orig_value is not None:
            provider = provider_registry.get_provider(provider_config['name'])(**provider_config)
            value = provider.alter_value(orig_value)
            append = column_definition.get('append')
            if append:
                value = value + append
            format = column_definition.get('format')
            if format:
                value = format.format(pga_value=value, **row)
            nested_set(row, full_column_name, value)
            column_dict[column_name] = nested_get(row, column_name)
    return column_dict


def truncate_tables(connection, tables):
    """
    Truncate a list of tables.

    :param connection: A database connection instance
    :param list[str] tables: A list of table names
    """
    if not tables:
        return
    cursor = connection.cursor()
    table_names = SQL(', ').join([Identifier(table_name) for table_name in tables])
    logging.info('Truncating tables "%s"', table_names)
    cursor.execute(SQL('TRUNCATE TABLE {tables}').format(tables=table_names).as_string(connection))
    cursor.close()


def create_database_dump(filename, db_args):
    """
    Create a dump file from the current database.

    :param str filename: Path to the dumpfile that should be created
    :param dict db_args: A dictionary with database related information
    """
    arguments = '-d {dbname} -U {user} -h {host} -p {port}'.format(**db_args)
    cmd = 'pg_dump -Fc -Z 9 {args} -f {filename}'.format(
        args=arguments,
        filename=filename
    )
    logging.info('Creating database dump file "%s"', filename)
    subprocess.call(cmd, shell=True)


def get_column_name(definition, fully_qualified=False):
    """
    Get column name by definition.

    :param dict definition: Column definition
    :param bool fully_qualified: Get complete column name with path (json objects)
    :return: A string, containing column name. ex:
        id
        name
        metadata.col1
    :rtype: string
    """
    col_name = list(definition.keys())[0]
    if fully_qualified:
        return col_name
    else:
        return col_name.split('.', 2)[0]


def get_column_names(definitions, fully_qualified=False):
    """Get distinct column names from definitions

    :param list definitions: A list of table definitions from the YAML schema.
    :return: A list of column names
    :rtype: list
    """
    names = []
    for definition in definitions:
        name = get_column_name(definition, fully_qualified)
        if name not in names:
            names.append(name)
    return names


def escape_str_replace(value):
    """Get escaped value

    :param Value to be encoded.
    :return: Escaped value
    :rtype: unknown
    """
    if isinstance(value, dict):
        return json.dumps(value).encode()
    return value


def nested_get(dic, path, delimiter='.'):
    """Get from dictionary by path

    :dic dict Source dictionaly.
    :path string Path withing dictionary
    :delimiter string Path delimiter
    :return: Value at path
    :rtype: unknown
    """
    try:
        keys = path.split(delimiter)
        for key in keys[:-1]:
            dic = dic.get(key, {})
        return dic[keys[-1]]
    except (AttributeError, KeyError, TypeError):
        return None


def nested_set(dic, path, value, delimiter='.'):
    """Set dictionary value by path

    :dic dict Source dictionaly.
    :path string Path withing dictionary
    :value unknow Value to be set
    :delimiter string Path delimiter
    """
    keys = path.split(delimiter)
    for key in keys[:-1]:
        dic = dic.get(key, {})
    dic[keys[-1]] = value


def load_config(schema: str) -> dict:
    # Original code from here https://gist.github.com/mkaranasou/ba83e25c835a8f7629e34dd7ede01931
    tag = '!ENV'
    pattern = re.compile(r'.*?\${(\w+)}.*?')
    custom_loader = yaml.FullLoader
    custom_loader.add_implicit_resolver(tag, pattern, None)

    def constructor_env_variables(loader, node):
        """
        Extracts the environment variable from the node's value
        :param yaml.Loader loader: the yaml loader
        :param node: the current node in the yaml
        :return: the parsed string that contains the value of the environment
        variable
        """
        value = loader.construct_scalar(node)
        match = pattern.findall(value)  # to find all env variables in line
        if match:
            full_value = value
            for g in match:
                full_value = full_value.replace(
                    f'${{{g}}}', os.environ.get(g, g)
                )
            return full_value
        return value

    custom_loader.add_constructor(tag, constructor_env_variables)
    return yaml.load(open(schema), Loader=custom_loader)
