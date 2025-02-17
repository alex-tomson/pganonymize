Schema
======

``pganonymize`` uses a YAML based schema definition for the anonymization rules.

Top level
---------

``tables``
~~~~~~~~~~

On the top level a list of tables can be defined with the ``tables`` keyword. This will define
which tables should be anonymized.

**Example**:

.. code-block:: yaml

    tables:
     - table_a:
        fields:
         - field_a: ...
         - field_b: ...
     - table_b:
        fields:
         - field_a: ...
         - field_b: ...

``truncate``
~~~~~~~~~~~~

You can also specify a list of tables that should be cleared instead of anonymized with the  `truncated` key. This is
useful if you don't need the table data for development purposes or the reduce the size of the database dump.

**Example**:

.. code-block:: yaml

    truncate:
     - django_session
     - my_other_table

If two tables have a foreign key relation and you don't need to keep one of the table's data, just add the
second table and they will be truncated at once, without causing a constraint error.

Table level
-----------

``primary_key``
~~~~~~~~~~~~~~~

Defines the name of the primary key field for the current table. The default is ``id``.

**Example**:

.. code-block:: yaml

    tables:
     - my_table:
        primary_key: my_primary_key
        fields: ...

``fields``
~~~~~~~~~~

Starting with the keyword ``fields`` you can specify all fields of a table, that should be available for the
anonymization process. Each field entry has its own ``provider`` that defines how the field should
be treated.

**Example**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - first_name:
            provider:
              name: clear
     - customer_email:
        fields:
         - email:
            provider:
              name: md5
            append: @localhost

``excludes``
~~~~~~~~~~~~

For each table you can also specify a list of ``excludes``. Each entry has to be a field name which contains
a list of exclude patterns. If one of these patterns matches, the whole table row won't be anonymized.

**Example**:

.. code-block:: yaml

    tables:
     - auth_user:
        primary_key: id
        fields:
         - first_name:
            provider:
              name: clear
        excludes:
         - email:
           - "\\S[^@]*@example\\.com"

This will exclude all data from the table ``auth_user`` that have an ``email`` field which matches the
regular expression pattern (the backslash is to escape the string for YAML).

``search``
~~~~~~~~~~

You can also specify a (SQL WHERE) `search_condition`, to filter the table for rows to be anonymized.
This is useful if you need to anonymize one or more specific records, eg for "Right to be forgotten" (GDPR etc) purpose.

**Example**:

.. code-block:: yaml

    tables:
     - auth_user:
        search: id BETWEEN 18 AND 140 AND user_type = 'customer'
        fields:
         - first_name:
            provider:
              name: clear

YAML schema file supports placeholders with environment variables, e.g.:

.. code-block:: bash

    !ENV ${HOST}
    !ENV '/var/${LOG_PATH}'

So you can construct dynamic filter conditions like:

.. code-block:: bash

    $ export COMPANY_ID=123
    $ export ACTION_TO_BE_TAKEN=clear
    $ pganonymize

**Example**:

.. code-block:: yaml

    - login:
        search: id = '!ENV ${COMPANY_ID}'
        search2: id = ${COMPANY_ID}
        search3: username = '${USER_TO_BE_SEARCHED}'
        fields:
         - first_name:
            provider:
              name: ${ACTION_TO_BE_TAKEN}

``chunk_size``
~~~~~~~~~~~~~~

Defines how many data rows should be fetched for each iteration of anonymizing the current table. The default is 2000.

**Example**:

.. code-block:: yaml

    tables:
     - auth_user:
        chunk_size: 5000
        fields: ...

Field level
-----------

``provider``
~~~~~~~~~~~~

Providers are the tools, which means functions, used to alter the data within the database. You can specify on field
level which provider should be used to alter the specific field. The reference a provider you will have can use the
``name`` attribute.

**Example**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - first_name:
            provider:
              name: set
              value: "Foo"


For a complete list of providers see the next section.

``append``
~~~~~~~~~~

This argument will append a value at the end of the altered value:

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - email:
            provider:
              name: md5
            append: "@example.com"


Provider
--------

``choice``
~~~~~~~~~~

This provider will define a list of possible values for a database field and will randomly make a choice
from this list.

**Arguments:**

* ``values``: All list of values

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - first_name:
            provider:
              name: choice
              values:
                - "John"
                - "Lisa"
                - "Tom"

``clear``
~~~~~~~~~

**Arguments:** none

The ``clear`` provider will set a database field to ``null``.

.. note::
   But remember, that you can set fields to ``null`` only if the database field allows null values.

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - first_name:
            provider:
              name: clear


``fake``
~~~~~~~~

**Arguments:** none

``pganonymize`` supports all providers from the Python library `Faker`_. All you have to do is prefix the provider with
``fake`` and then use the function name from the Faker library, e.g:

* ``fake.first_name``
* ``fake.street_name``

.. note::
   Please note: using the ``Faker`` library will generate randomly generated data for each data row
   within a table. This will dramatically slow down the anonymization process.

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - email:
            provider:
              name: fake.email

See the `Faker documentation`_ for a full set of providers.

``mask``
~~~~~~~~

**Arguments:**

* ``sign``: The sign to be used to replace the original characters (default ``X``).

This provider will replace each character with a static sign.

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - last_name:
            provider:
              name: mask
              sign: '?'


``md5``
~~~~~~~

**Arguments:**

* ``as_number`` (default ``False``): Return the MD5 hash as an integer.
* ``as_number_length`` (default 8): The length of the integer representation.

This provider will hash the given field value with the MD5 algorithm.

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - password:
            provider:
              name: md5
              as_number: True


``set``
~~~~~~~

**Arguments:**

* ``value``: The value to set

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - first_name:
            provider:
              name: set
              value: "Foo"

The value can also be a dictionary for JSONB columns:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - first_name:
            provider:
              name: set
              value: '{"foo": "bar", "baz": 1}'


``uuid4``
~~~~~~~~~

**Arguments:** none

This provider will replace values with a unique UUID4.

.. note::
   The provider will only generate `native UUIDs`_. If you want to use UUIDs for character based columns, use
   ``fake.uuid4`` instead.

**Example usage**:

.. code-block:: yaml

    tables:
     - auth_user:
        fields:
         - first_name:
            provider:
              name: uuid4

.. _Faker: https://github.com/joke2k/faker
.. _Faker documentation: http://faker.rtfd.org/
.. _native UUIDs: https://www.postgresql.org/docs/current/datatype-uuid.html
