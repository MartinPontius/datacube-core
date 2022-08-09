# This file is part of the Open Data Cube, see https://opendatacube.org for more information
#
# Copyright (c) 2015-2020 ODC Contributors
# SPDX-License-Identifier: Apache-2.0

# We often have one-arg-per column, so these checks aren't so useful.
# pylint: disable=too-many-arguments,too-many-public-methods,too-many-lines

# SQLAlchemy queries require "column == None", not "column is None" due to operator overloading:
# pylint: disable=singleton-comparison

"""
Persistence API implementation for postgis.
"""

import logging
import uuid  # noqa: F401
from sqlalchemy import cast
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select, text, and_, or_, func
from sqlalchemy.dialects.postgresql import INTERVAL
from typing import Iterable, Tuple
from datacube.index.fields import OrExpression
from datacube.model import Range
from . import _core
from . import _dynamic as dynamic
from ._fields import parse_fields, Expression, PgField, PgExpression  # noqa: F401
from ._fields import NativeField, DateDocField, SimpleDocField
from ._schema import MetadataType, Product,  \
    Dataset, DatasetSource, DatasetLocation, SelectedDatasetLocation
from .sql import escape_pg_identifier


# Make a function because it's broken
def _dataset_select_fields():
    return (
        Dataset,
        # All active URIs, from newest to oldest
        func.array(
            select(
                SelectedDatasetLocation.uri
            ).where(
                and_(
                    SelectedDatasetLocation.dataset_ref == Dataset.id,
                    SelectedDatasetLocation.archived == None
                )
            ).order_by(
                SelectedDatasetLocation.added.desc(),
                SelectedDatasetLocation.id.desc()
            ).label('uris')
        ).label('uris')
    )


PGCODE_UNIQUE_CONSTRAINT = '23505'
PGCODE_FOREIGN_KEY_VIOLATION = '23503'

_LOG = logging.getLogger(__name__)


def _split_uri(uri):
    """
    Split the scheme and the remainder of the URI.

    """
    idx = uri.find(':')
    if idx < 0:
        raise ValueError("Not a URI")

    return uri[:idx], uri[idx+1:]


def get_native_fields():
    # Native fields (hard-coded into the schema)
    fields = {
        'id': NativeField(
            'id',
            'Dataset UUID',
            Dataset.id
        ),
        'indexed_time': NativeField(
            'indexed_time',
            'When dataset was indexed',
            Dataset.added
        ),
        'indexed_by': NativeField(
            'indexed_by',
            'User who indexed the dataset',
            Dataset.added_by
        ),
        'product': NativeField(
            'product',
            'Product name',
            Product.name
        ),
        'product_id': NativeField(
            'product_id',
            'ID of a dataset type',
            Dataset.product_ref
        ),
        'metadata_type': NativeField(
            'metadata_type',
            'Metadata type name of dataset',
            MetadataType.name
        ),
        'metadata_type_id': NativeField(
            'metadata_type_id',
            'ID of a metadata type',
            Dataset.metadata_type_ref
        ),
        'metadata_doc': NativeField(
            'metadata_doc',
            'Full metadata document',
            Dataset.metadata_doc
        ),
        # Fields that can affect row selection

        # Note that this field is a single uri: selecting it will result in one-result per uri.
        # (ie. duplicate datasets if multiple uris, no dataset if no uris)
        'uri': NativeField(
            'uri',
            "Dataset URI",
            DatasetLocation.uri_body,
            alchemy_expression=DatasetLocation.uri,
            affects_row_selection=True
        ),
    }
    return fields


def get_dataset_fields(metadata_type_definition):
    dataset_section = metadata_type_definition['dataset']

    fields = get_native_fields()
    # "Fixed fields" (not dynamic: defined in metadata type schema)
    fields.update(dict(
        creation_time=DateDocField(
            'creation_time',
            'Time when dataset was created (processed)',
            Dataset.metadata_doc,
            False,
            offset=dataset_section.get('creation_dt') or ['creation_dt']
        ),
        format=SimpleDocField(
            'format',
            'File format (GeoTiff, NetCDF)',
            Dataset.metadata_doc,
            False,
            offset=dataset_section.get('format') or ['format', 'name']
        ),
        label=SimpleDocField(
            'label',
            'Label',
            Dataset.metadata_doc,
            False,
            offset=dataset_section.get('label') or ['label']
        ),
    ))

    # noinspection PyTypeChecker
    fields.update(
        parse_fields(
            dataset_section['search_fields'],
            Dataset.metadata_doc
        )
    )
    return fields


class PostgisDbAPI(object):
    def __init__(self, connection):
        self._connection = connection

    @property
    def in_transaction(self):
        return self._connection.in_transaction()

    def rollback(self):
        self._connection.execute(text('ROLLBACK'))

    def execute(self, command):
        return self._connection.execute(command)

    def insert_dataset(self, metadata_doc, dataset_id, product_id):
        """
        Insert dataset if not already indexed.
        :type metadata_doc: dict
        :type dataset_id: str or uuid.UUID
        :type product_id: int
        :return: whether it was inserted
        :rtype: bool
        """
        metadata_subquery = select(Product.metadata_type_ref).where(Product.id == product_id).scalar_subquery()
        ret = self._connection.execute(
            insert(Dataset).values(
                id=dataset_id,
                product_ref=product_id,
                metadata=metadata_doc,
                metadata_type_ref=metadata_subquery
            ).on_conflict_do_nothing(
                index_elements=['id']
            )
        )
        return ret.rowcount > 0

    def update_dataset(self, metadata_doc, dataset_id, product_id):
        """
        Update dataset
        :type metadata_doc: dict
        :type dataset_id: str or uuid.UUID
        :type product_id: int
        """
        res = self._connection.execute(
            update(Dataset).returning(Dataset.id).where(
                Dataset.id == dataset_id
            ).where(
                Dataset.product_ref == product_id
            ).values(
                metadata=metadata_doc
            )
        )
        return res.rowcount > 0

    def insert_dataset_location(self, dataset_id, uri):
        """
        Add a location to a dataset if it is not already recorded.

        Returns True if success, False if this location already existed

        :type dataset_id: str or uuid.UUID
        :type uri: str
        :rtype bool:
        """

        scheme, body = _split_uri(uri)

        r = self._connection.execute(
            insert(DatasetLocation).on_conflict_do_nothing(
                index_elements=['uri_scheme', 'uri_body', 'dataset_ref']
            ).values(
                dataset_ref=dataset_id,
                uri_scheme=scheme,
                uri_body=body,
            )
        )

        return r.rowcount > 0

    def contains_dataset(self, dataset_id):
        return bool(
            self._connection.execute(
                select(Dataset.id).where(
                    Dataset.id == dataset_id
                )
            ).fetchone()
        )

    def datasets_intersection(self, dataset_ids):
        """ Compute set intersection: db_dataset_ids & dataset_ids
        """
        return [ds.id for ds in self._connection.execute(
                select(
                    Dataset.id
                ).where(
                    Dataset.id.in_(dataset_ids)
                )
            ).fetchall()
        ]

    def get_datasets_for_location(self, uri, mode=None):
        scheme, body = _split_uri(uri)

        if mode is None:
            mode = 'exact' if body.count('#') > 0 else 'prefix'

        if mode == 'exact':
            body_query = DatasetLocation.uri_body == body
        elif mode == 'prefix':
            body_query = DatasetLocation.uri_body.startswith(body)
        else:
            raise ValueError('Unsupported query mode {}'.format(mode))

        return self._connection.execute(
            select(
                _dataset_select_fields()
            ).join(
                Dataset.locations
            ).where(
                and_(DatasetLocation.uri_scheme == scheme, body_query)
            )
        ).fetchall()

    def all_dataset_ids(self, archived: bool):
        query = select(Dataset.id)
        if archived:
            query = query.where(
                Dataset.archived != None
            )
        else:
            query = query.where(
                Dataset.archived == None
            )
        return self._connection.execute(query).fetchall()

    def insert_dataset_source(self, classifier, dataset_id, source_dataset_id):
        r = self._connection.execute(
            insert(DatasetSource).on_conflict_do_nothing(
                index_elements=['classifier', 'dataset_ref']
            ).values(
                classifier=classifier,
                dataset_ref=dataset_id,
                source_dataset_ref=source_dataset_id
            )
        )
        return r.rowcount > 0

    def archive_dataset(self, dataset_id):
        r = self._connection.execute(
            update(Dataset).where(
                Dataset.id == dataset_id
            ).where(
                Dataset.archived == None
            ).values(
                archived=func.now()
            )
        )
        return r.rowcount > 0

    def restore_dataset(self, dataset_id):
        r = self._connection.execute(
            update(Dataset).where(
                Dataset.id == dataset_id
            ).values(
                archived=None
            )
        )
        return r.rowcount > 0

    def delete_dataset(self, dataset_id):
        self._connection.execute(
            delete(DatasetLocation).where(
                DatasetLocation.dataset_ref == dataset_id
            )
        )
        self._connection.execute(
            delete(DatasetSource).where(
                DatasetSource.dataset_ref == dataset_id
            )
        )
        r = self._connection.execute(
            delete(Dataset).where(
                Dataset.id == dataset_id
            )
        )
        return r.rowcount > 0

    def get_dataset(self, dataset_id):
        return self._connection.execute(
            select(_dataset_select_fields()).where(Dataset.id == dataset_id)
        ).first()

    def get_datasets(self, dataset_ids):
        return self._connection.execute(
            select(_dataset_select_fields()).where(Dataset.id.in_(dataset_ids))
        ).fetchall()

    def get_derived_datasets(self, dataset_id):
        raise NotImplementedError

    def get_dataset_sources(self, dataset_id):
        raise NotImplementedError

    def search_datasets_by_metadata(self, metadata):
        """
        Find any datasets that have the given metadata.

        :type metadata: dict
        :rtype: dict
        """
        # Find any storage types whose 'dataset_metadata' document is a subset of the metadata.
        return self._connection.execute(
            select(_dataset_select_fields()).where(Dataset.metadata_doc.contains(metadata))
        ).fetchall()

    @staticmethod
    def _alchemify_expressions(expressions):
        def raw_expr(expression):
            if isinstance(expression, OrExpression):
                return or_(raw_expr(expr) for expr in expression.exprs)
            return expression.alchemy_expression

        return [raw_expr(expression) for expression in expressions]

    @staticmethod
    def search_datasets_query(expressions, source_exprs=None,
                              select_fields=None, with_source_ids=False, limit=None):
        """
        :type expressions: Tuple[Expression]
        :type source_exprs: Tuple[Expression]
        :type select_fields: Iterable[PgField]
        :type with_source_ids: bool
        :type limit: int
        :rtype: sqlalchemy.Expression
        """
        # TODO: lineage handling and source search
        assert source_exprs is None
        assert not with_source_ids

        if select_fields:
            select_columns = tuple(
                f.alchemy_expression.label(f.name)
                for f in select_fields
            )
        else:
            select_columns = _dataset_select_fields()

        raw_expressions = PostgisDbAPI._alchemify_expressions(expressions)
        join_tables = PostgisDbAPI._join_tables(Dataset, expressions, select_fields)
        where_expr = and_(Dataset.archived == None, *raw_expressions)

        query = select(select_columns).select_from(Dataset)
        for join in join_tables:
            query = query.join(join)
        query = query.where(where_expr).limit(limit)
        return query

    def search_datasets(self, expressions,
                        source_exprs=None, select_fields=None,
                        with_source_ids=False, limit=None):
        """
        :type with_source_ids: bool
        :type select_fields: tuple[datacube.drivers.postgis._fields.PgField]
        :type expressions: tuple[datacube.drivers.postgis._fields.PgExpression]
        """
        select_query = self.search_datasets_query(expressions, source_exprs,
                                                  select_fields, with_source_ids, limit)
        _LOG.debug("search_datasets SQL: %s", str(select_query))
        return self._connection.execute(select_query)

    @staticmethod
    def search_unique_datasets_query(expressions, select_fields, limit):
        """
        'unique' here refer to that the query results do not contain datasets
        having the same 'id' more than once.

        We are not dealing with dataset_source table here and we are not joining
        dataset table with dataset_location table. We are aggregating stuff
        in dataset_location per dataset basis if required. It returns the construted
        query.
        """
        # TODO
        raise NotImplementedError()

    def search_unique_datasets(self, expressions, select_fields=None, limit=None):
        """
        Processes a search query without duplicating datasets.

        'unique' here refer to that the results do not contain datasets having the same 'id'
        more than once. we achieve this by not allowing dataset table to join with
        dataset_location or dataset_source tables. Joining with other tables would not
        result in multiple records per dataset due to the direction of cardinality.
        """

        select_query = self.search_unique_datasets_query(expressions, select_fields, limit)

        return self._connection.execute(select_query)

    def get_duplicates(self, match_fields, expressions):
        # TODO
        # type: (Tuple[PgField], Tuple[PgExpression]) -> Iterable[tuple]
        group_expressions = tuple(f.alchemy_expression for f in match_fields)
        join_tables = PostgisDbAPI._join_tables(Dataset, expressions, match_fields)

        query = select(
            (func.array_agg(Dataset.id),) + group_expressions
        ).select_from(Dataset)
        for join in join_tables:
            query = query.join(join)

        query = query.where(
            and_(Dataset.archived == None, *(PostgisDbAPI._alchemify_expressions(expressions)))
        ).group_by(
            *group_expressions
        ).having(
            func.count(Dataset.id) > 1
        )
        return self._connection.execute(query)

    def count_datasets(self, expressions):
        """
        :type expressions: tuple[datacube.drivers.postgis._fields.PgExpression]
        :rtype: int
        """

        raw_expressions = self._alchemify_expressions(expressions)

        select_query = (
            select(
                func.count(Dataset.id)
            ).where(
                Dataset.archived == None
            ).where(
                *raw_expressions
            )
        )
        return self._connection.scalar(select_query)

    def count_datasets_through_time(self, start, end, period, time_field, expressions):
        """
        :type period: str
        :type start: datetime.datetime
        :type end: datetime.datetime
        :type expressions: tuple[datacube.drivers.postgis._fields.PgExpression]
        :rtype: list[((datetime.datetime, datetime.datetime), int)]
        """

        results = self._connection.execute(
            self.count_datasets_through_time_query(start, end, period, time_field, expressions)
        )

        for time_period, dataset_count in results:
            # if not time_period.upper_inf:
            yield Range(time_period.lower, time_period.upper), dataset_count

    def count_datasets_through_time_query(self, start, end, period, time_field, expressions):
        raw_expressions = self._alchemify_expressions(expressions)

        start_times = select((
            func.generate_series(start, end, cast(period, INTERVAL)).label('start_time'),
        )).alias('start_times')

        time_range_select = (
            select((
                func.tstzrange(
                    start_times.c.start_time,
                    func.lead(start_times.c.start_time).over()
                ).label('time_period'),
            ))
        ).alias('all_time_ranges')

        # Exclude the trailing (end time to infinite) row. Is there a simpler way?
        time_ranges = (
            select((
                time_range_select,
            )).where(
                ~func.upper_inf(time_range_select.c.time_period)
            )
        ).alias('time_ranges')

        count_query = select(func.count('*'))
        join_tables = self._join_tables(Dataset, expressions)
        for join in join_tables:
            count_query = count_query.join(join)
        count_query = count_query.where(
            and_(
                time_field.alchemy_expression.overlaps(time_ranges.c.time_period),
                Dataset.archived == None,
                *raw_expressions
            )
        )

        return select((time_ranges.c.time_period, count_query.label('dataset_count')))

    @staticmethod
    def _join_tables(source_table, expressions=None, fields=None):
        join_tables = set()
        if expressions:
            join_tables.update(expression.field.required_alchemy_table for expression in expressions)
        if fields:
            join_tables.update(field.required_alchemy_table for field in fields)
        join_tables.discard(source_table.__table__)
        # TODO: Current architecture must sort-hack.  Better join awareness required at field level.
        sort_order_hack = [DatasetLocation, Dataset, Product, MetadataType]
        return [
            orm_table
            for orm_table in sort_order_hack
            if orm_table.__table__ in join_tables
        ]

    def get_product(self, id_):
        return self._connection.execute(
            select(Product).where(Product.id == id_)
        ).first()

    def get_metadata_type(self, id_):
        return self._connection.execute(
            select(MetadataType).where(MetadataType.id == id_)
        ).first()

    def get_product_by_name(self, name):
        return self._connection.execute(
            select(Product).where(Product.name == name)
        ).first()

    def get_metadata_type_by_name(self, name):
        return self._connection.execute(
            select(MetadataType).where(MetadataType.name == name)
        ).first()

    def insert_product(self,
                       name,
                       metadata,
                       metadata_type_id,
                       search_fields,
                       definition,
                       concurrently=True):

        res = self._connection.execute(
            insert(Product).values(
                name=name,
                metadata=metadata,
                metadata_type_ref=metadata_type_id,
                definition=definition
            )
        )

        type_id = res.inserted_primary_key[0]

        # Initialise search fields.
        # TODO: Isn't definition['metadata'] the same as metadata?
        self._setup_product_fields(type_id, name, search_fields, definition['metadata'],
                                   concurrently=concurrently)
        return type_id

    def update_product(self,
                       name,
                       metadata,
                       metadata_type_id,
                       search_fields,
                       definition,
                       update_metadata_type=False, concurrently=False):
        # TODO: Isn't definition['metadata'] the same as metadata?
        res = self._connection.execute(
            update(Product).returning(Product.id).where(
                Product.name == name
            ).values(
                metadata=metadata,
                metadata_type_ref=metadata_type_id,
                definition=definition
            )
        )
        prod_id = res.first()[0]

        if update_metadata_type:
            if not self._connection.in_transaction():
                raise RuntimeError('Must update metadata types in transaction')

            self._connection.execute(
                update(Dataset).where(
                    Dataset.product_ref == prod_id
                ).values(
                    metadata_type_ref=metadata_type_id,
                )
            )

        # Initialise search fields.
        # TODO: Isn't definition['metadata'] the same as metadata?
        self._setup_product_fields(prod_id, name, search_fields, definition['metadata'],
                                   concurrently=concurrently,
                                   rebuild_view=True)
        return prod_id

    def insert_metadata_type(self, name, definition, concurrently=False):
        res = self._connection.execute(
            insert(MetadataType).values(
                name=name,
                definition=definition
            )
        )
        type_id = res.inserted_primary_key[0]

        search_fields = get_dataset_fields(definition)
        self._setup_metadata_type_fields(
            type_id, name, search_fields, concurrently=concurrently
        )

    def update_metadata_type(self, name, definition, concurrently=False):
        res = self._connection.execute(
            update(MetadataType).returning(MetadataType.id).where(
                MetadataType.name == name
            ).values(
                name=name,
                definition=definition
            )
        )
        type_id = res.first()[0]

        search_fields = get_dataset_fields(definition)
        self._setup_metadata_type_fields(
            type_id, name, search_fields,
            concurrently=concurrently,
            rebuild_views=True,
        )

        return type_id

    def check_dynamic_fields(self, concurrently=False, rebuild_views=False, rebuild_indexes=False):
        _LOG.info('Checking dynamic views/indexes. (rebuild views=%s, indexes=%s)', rebuild_views, rebuild_indexes)

        search_fields = {}

        for metadata_type in self.get_all_metadata_types():
            fields = get_dataset_fields(metadata_type['definition'])
            search_fields[metadata_type['id']] = fields
            self._setup_metadata_type_fields(
                metadata_type['id'],
                metadata_type['name'],
                fields,
                rebuild_indexes=rebuild_indexes,
                rebuild_views=rebuild_views,
                concurrently=concurrently,
            )

    def _setup_metadata_type_fields(self, id_, name, fields,
                                    rebuild_indexes=False, rebuild_views=False, concurrently=True):
        for product in self._get_products_for_metadata_type(id_):
            self._setup_product_fields(
                product['id'],
                product['name'],
                fields,
                product['definition']['metadata'],
                rebuild_view=rebuild_views,
                rebuild_indexes=rebuild_indexes,
                concurrently=concurrently
            )

    def _setup_product_fields(self, id_, name, fields, metadata_doc,
                              rebuild_indexes=False, rebuild_view=False, concurrently=True):
        dataset_filter = and_(Dataset.archived == None, Dataset.product_ref == id_)
        excluded_field_names = tuple(self._get_active_field_names(fields, metadata_doc))

        dynamic.check_dynamic_fields(self._connection, concurrently, dataset_filter,
                                     excluded_field_names, fields, name,
                                     rebuild_indexes=rebuild_indexes, rebuild_view=rebuild_view)

    @staticmethod
    def _get_active_field_names(fields, metadata_doc):
        for field in fields.values():
            if hasattr(field, 'extract'):
                try:
                    value = field.extract(metadata_doc)
                    if value is not None:
                        yield field.name
                except (AttributeError, KeyError, ValueError):
                    continue

    def get_all_products(self):
        return self._connection.execute(
            select(Product).order_by(Product.name.asc())
        ).fetchall()

    def _get_products_for_metadata_type(self, id_):
        return self._connection.execute(
            select(Product).where(
                Product.metadata_type_ref == id_
            ).order_by(
                Product.name.asc()
            )).fetchall()

    def get_all_metadata_types(self):
        return self._connection.execute(select(MetadataType).order_by(MetadataType.name.asc())).fetchall()

    def get_locations(self, dataset_id):
        return [
            record[0]
            for record in self._connection.execute(
                select(
                    DatasetLocation.uri
                ).where(
                    DatasetLocation.dataset_ref == dataset_id
                ).where(
                    DatasetLocation.archived == None
                ).order_by(
                    DatasetLocation.added.desc(),
                    DatasetLocation.id.desc()
                )
            ).fetchall()
        ]

    def get_archived_locations(self, dataset_id):
        """
        Return a list of uris and archived_times for a dataset
        """
        return [
            (location_uri, archived_time)
            for location_uri, archived_time in self._connection.execute(
                select(
                    DatasetLocation.uri, DatasetLocation.archived
                ).where(
                    DatasetLocation.dataset_ref == dataset_id
                ).where(
                    DatasetLocation.archived != None
                ).order_by(
                    DatasetLocation.added.desc()
                )
            ).fetchall()
        ]

    def remove_location(self, dataset_id, uri):
        """
        Remove the given location for a dataset

        :returns bool: Was the location deleted?
        """
        scheme, body = _split_uri(uri)
        res = self._connection.execute(
            delete(DatasetLocation).where(
                DatasetLocation.dataset_ref == dataset_id
            ).where(
                DatasetLocation.uri_scheme == scheme
            ).where(
                DatasetLocation.uri_body == body
            )
        )
        return res.rowcount > 0

    def archive_location(self, dataset_id, uri):
        scheme, body = _split_uri(uri)
        res = self._connection.execute(
            update(DatasetLocation).where(
                DatasetLocation.dataset_ref == dataset_id
            ).where(
                DatasetLocation.uri_scheme == scheme
            ).where(
                DatasetLocation.uri_body == body
            ).where(
                DatasetLocation.archived == None
            ).values(
                archived=func.now()
            )
        )
        return res.rowcount > 0

    def restore_location(self, dataset_id, uri):
        scheme, body = _split_uri(uri)
        res = self._connection.execute(
            update(DatasetLocation).where(
                DatasetLocation.dataset_ref == dataset_id
            ).where(
                DatasetLocation.uri_scheme == scheme
            ).where(
                DatasetLocation.uri_body == body
            ).where(
                DatasetLocation.archived != None
            ).values(
                archived=None
            )
        )
        return res.rowcount > 0

    def __repr__(self):
        return "PostgresDb<connection={!r}>".format(self._connection)

    def list_users(self):
        result = self._connection.execute("""
            select
                group_role.rolname as role_name,
                user_role.rolname as user_name,
                pg_catalog.shobj_description(user_role.oid, 'pg_authid') as description
            from pg_roles group_role
            inner join pg_auth_members am on am.roleid = group_role.oid
            inner join pg_roles user_role on am.member = user_role.oid
            where (group_role.rolname like 'odc_%%') and not (user_role.rolname like 'odc_%%')
            order by group_role.oid asc, user_role.oid asc;
        """)
        for row in result:
            yield _core.from_pg_role(row['role_name']), row['user_name'], row['description']

    def create_user(self, username, password, role, description=None):
        pg_role = _core.to_pg_role(role)
        username = escape_pg_identifier(self._connection, username)
        sql = text('create user {username} password :password in role {role}'.format(username=username, role=pg_role))
        self._connection.execute(sql,
                                 password=password)
        if description:
            sql = text('comment on role {username} is :description'.format(username=username))
            self._connection.execute(sql,
                                     description=description)

    def drop_users(self, users):
        # type: (Iterable[str]) -> None
        for username in users:
            sql = text('drop role {username}'.format(username=escape_pg_identifier(self._connection, username)))
            self._connection.execute(sql)

    def grant_role(self, role, users):
        # type: (str, Iterable[str]) -> None
        """
        Grant a role to a user.
        """
        pg_role = _core.to_pg_role(role)

        for user in users:
            if not _core.has_role(self._connection, user):
                raise ValueError('Unknown user %r' % user)

        _core.grant_role(self._connection, pg_role, users)
