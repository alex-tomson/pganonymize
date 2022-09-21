import shlex
from argparse import Namespace

import pytest
from mock import Mock, call, patch

from pganonymize.cli import get_arg_parser, main
from pganonymize.exceptions import BadSchemaFormat
from tests.utils import quote_ident


class TestCli:
    @patch("psycopg2.extensions.quote_ident", side_effect=quote_ident)
    @patch("pganonymize.utils.psycopg2.connect")
    @patch("pganonymize.utils.subprocess")
    @pytest.mark.parametrize(
        "cli_args, expected, expected_executes, commit_calls, call_dump",
        [
            [
                "--host localhost --port 5432 --user root --password my-cool-password --dbname db --schema ./tests/schemes/valid_schema.yml -v --init-sql \"set work_mem='1GB'\"",  # noqa
                Namespace(
                    verbose=1,
                    list_providers=False,
                    schema="./tests/schemes/valid_schema.yml",
                    dbname="db",
                    user="root",
                    password="my-cool-password",
                    host="localhost",
                    port="5432",
                    dry_run=False,
                    dump_file=None,
                    init_sql="set work_mem='1GB'",
                ),  # noqa
                [
                    call("SET search_path TO db;"),
                    call("set work_mem='1GB'"),
                    call('TRUNCATE TABLE "django_session"'),
                    call('SELECT COUNT(*) FROM "auth_user"'),
                    call(
                        'SELECT "id", "first_name", "last_name", "email" FROM "auth_user"'
                    ),
                    call(
                        'CREATE TEMP TABLE "tmp_auth_user" AS SELECT "id", "first_name", "last_name", "email"\n'
                        '                    FROM "auth_user" WITH NO DATA'
                    ),
                    # noqa
                    call('CREATE INDEX ON "tmp_auth_user" ("id")'),
                    call(
                        'UPDATE "auth_user" t '
                        'SET "first_name" = s."first_name", "last_name" = s."last_name", "email" = s."email" '
                        'FROM "tmp_auth_user" s WHERE t."id" = s."id"'
                    ),
                ],
                1,
                [],
            ],
            [
                "--dry-run --host localhost --port 5432 --user root --password my-cool-password "
                "--dbname db --schema ./tests/schemes/valid_schema.yml -v --init-sql \"set work_mem='1GB'\"",
                Namespace(
                    verbose=1,
                    list_providers=False,
                    schema="./tests/schemes/valid_schema.yml",
                    dbname="db",
                    user="root",
                    password="my-cool-password",
                    host="localhost",
                    port="5432",
                    dry_run=True,
                    dump_file=None,
                    init_sql="set work_mem='1GB'",
                ),  # noqa
                [
                    call("SET search_path TO db;"),
                    call("set work_mem='1GB'"),
                    call('TRUNCATE TABLE "django_session"'),
                    call(
                        'SELECT "id", "first_name", "last_name", "email" FROM "auth_user" LIMIT 100'
                    ),
                    call(
                        'CREATE TEMP TABLE "tmp_auth_user" AS SELECT "id", "first_name", "last_name", "email"\n'
                        '                    FROM "auth_user" WITH NO DATA'
                    ),
                    call('CREATE INDEX ON "tmp_auth_user" ("id")'),
                    call(
                        'UPDATE "auth_user" t '
                        'SET "first_name" = s."first_name", "last_name" = s."last_name", "email" = s."email" '
                        'FROM "tmp_auth_user" s WHERE t."id" = s."id"'
                    ),
                ],
                0,
                [],
            ],
            [
                "--dump-file ./dump.sql --host localhost --port 5432 --user root --password my-cool-password "
                "--dbname db --schema ./tests/schemes/valid_schema.yml -v --init-sql \"set work_mem='1GB'\"",
                # noqa
                Namespace(
                    verbose=1,
                    list_providers=False,
                    schema="./tests/schemes/valid_schema.yml",
                    dbname="db",
                    user="root",
                    password="my-cool-password",
                    host="localhost",
                    port="5432",
                    dry_run=False,
                    dump_file="./dump.sql",
                    init_sql="set work_mem='1GB'",
                ),
                [
                    call("SET search_path TO db;"),
                    call("set work_mem='1GB'"),
                    call('TRUNCATE TABLE "django_session"'),
                    call('SELECT COUNT(*) FROM "auth_user"'),
                    call(
                        'SELECT "id", "first_name", "last_name", "email" FROM "auth_user"'
                    ),
                    call(
                        'CREATE TEMP TABLE "tmp_auth_user" AS SELECT "id", "first_name", "last_name", "email"\n'
                        '                    FROM "auth_user" WITH NO DATA'
                    ),
                    call('CREATE INDEX ON "tmp_auth_user" ("id")'),
                    call(
                        'UPDATE "auth_user" t '
                        'SET "first_name" = s."first_name", "last_name" = s."last_name", "email" = s."email" '
                        'FROM "tmp_auth_user" s WHERE t."id" = s."id"'
                    ),
                ],
                1,
                [
                    call(
                        "pg_dump -Fc -Z 9 -d db -U root -h localhost -p 5432 -f ./dump.sql",
                        shell=True,
                    )
                ],
            ],
            [
                "--list-providers",
                Namespace(
                    verbose=None,
                    list_providers=True,
                    schema="schema.yml",
                    dbname=None,
                    user=None,
                    password="",
                    host="localhost",
                    port="5432",
                    dry_run=False,
                    dump_file=None,
                    init_sql=False,
                ),
                [],
                0,
                [],
            ],
        ],
    )
    def test_cli_args(
        self,
        subprocess,
        patched_connect,
        quote_ident,
        cli_args,
        expected,
        expected_executes,
        commit_calls,
        call_dump,
    ):
        arg_parser = get_arg_parser()
        parsed_args = arg_parser.parse_args(shlex.split(cli_args))
        assert parsed_args == expected
        mock_cursor = Mock()
        mock_cursor.fetchone.return_value = [0]
        mock_cursor.fetchmany.return_value = None

        connection = Mock()
        connection.cursor.return_value = mock_cursor

        patched_connect.return_value = connection
        main(parsed_args)
        assert mock_cursor.execute.call_args_list == expected_executes
        assert connection.commit.call_count == commit_calls

        assert subprocess.call.call_args_list == call_dump

    @patch("pganonymize.utils.psycopg2.connect")
    @pytest.mark.parametrize(
        "cli_args",
        [
            "--host localhost --port 5432 --user root --password my-cool-password --dbname db "
            "--schema ./tests/schemes/invalid_schema2.yml -v --init-sql \"set work_mem='1GB'\"",
        ],
    )
    def test_invalid_schema(self, patched_connect, cli_args):
        exc_text = "One of target_schema or overwrite_values_in_source_tables parameters must be defined!"
        arg_parser = get_arg_parser()
        parsed_args = arg_parser.parse_args(shlex.split(cli_args))
        with pytest.raises(BadSchemaFormat) as exc_info:
            main(parsed_args)
        assert exc_info.value.args[0] == exc_text
