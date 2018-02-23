import json
import sys
import time
import uuid

from asclepias_broker.datastore import Relationship, Relation, Identifier,\
    Group, GroupType, Identifier2Group, GroupM2M, GroupRelationship, \
    GroupRelationshipM2M, Relationship2GroupRelationship
from asclepias_broker.tasks import get_or_create_groups

import jsonschema

#
# Events generation helpers
#
EVENT_TYPE_MAP = {'C': 'relationship_created', 'D': 'relationship_deleted'}
SCHOLIX_RELATIONS = {'References', 'IsReferencedBy', 'IsSupplementTo',
                     'IsSupplementedBy'}
RELATIONS_ENUM = [
    'References', 'IsReferencedBy', 'IsSupplementTo', 'IsSupplementedBy',
    'IsIdenticalTo', 'Cites', 'IsCitedBy', 'IsVersionOf', 'HasVersion']

INPUT_ITEMS_SCHEMA = {
    'definitions': {
        'Relationship': {
            'type': 'array',
            'items': [
                {'type': 'string', 'title': 'Event type', 'enum': ['C', 'D']},
                {'type': 'string', 'title': 'Source identifier'},
                {'type': 'string', 'title': 'Relation',
                 'enum': RELATIONS_ENUM},
                {'type': 'string', 'title': 'Target identifier'},
                {'type': 'string', 'title': 'Publication Date'},
            ],
        },
    },
    'type': 'array',
    'items': {
        'oneOf': [
            # Allow nested, multi-payload events
            {'type': 'array', 'items': {'$ref': '#/definitions/Relationship'}},
            {'$ref': '#/definitions/Relationship'},
        ],
    }
}


class Event:
    """Event creation helper class."""

    def __init__(self, **kwargs):
        self.id = kwargs.get('id', str(uuid.uuid4()))
        self.time = kwargs.get('time', str(int(time.time())))
        self.payloads = kwargs.get('payloads', [])
        self.event_type = kwargs.get('event_type', 'relationship_created')
        self.creator = kwargs.get('creator', 'ACME Inc.')
        self.source = kwargs.get('source', 'Test')

    def _gen_identifier(self, identifier, scheme=None, url=None):
        d = {'ID': identifier, 'IDScheme': scheme or 'DOI'}
        if url:
            d['IDURL'] = url
        return d

    def _gen_object(self, source, type_=None, title=None, creator=None):
        return {
            'Identifier': self._gen_identifier(source),
            'Type': type_ or {'Name': 'unknown'},
        }

    def _gen_relation(self, relation):
        if relation not in SCHOLIX_RELATIONS:
            return {'Name': 'IsRelatedTo', 'SubType': relation,
                    'SubTypeSchema': 'DataCite'}
        return {'Name': relation}

    def add_payload(self, source, relation, target, publication_date,
                    provider=None):
        self.payloads.append({
            'Source': self._gen_object(source),
            'RelationshipType': self._gen_relation(relation),
            'Target': self._gen_object(target),
            'LinkPublicationDate': publication_date,
            'LinkProvider': [provider or {'Name': 'Link Provider Ltd.'}]
        })
        return self

    @property
    def event(self):
        return {
            'id': self.id,
            'event_type': self.event_type,
            'time': self.time,
            'creator': self.creator,
            'source': self.source,
            'payload': self.payloads,
        }


def generate_payloads(input_items, event_schema=None):
    jsonschema.validate(input_items, INPUT_ITEMS_SCHEMA)
    events = []
    for item in input_items:
        if isinstance(item[0], str):  # Single payload
            payloads = [item]
        else:  # Multiple payloads/relations
            payloads = item

        evt = Event(event_type=EVENT_TYPE_MAP[payloads[0][0]])
        for op, src, rel, trg, at in payloads:
            evt.add_payload(src, rel, trg, at)
        events.append(evt.event)
    if event_schema:
        jsonschema.validate(events, {'type': 'array', 'items': event_schema})
    return events


def create_objects_from_relations(session, relationships):
    """Given a list of relationships, create all corresponding DB objects.

    E.g.:
        relationships = [
            'A', Relation.Cites, 'B',
            'C', Relation.Cites, 'D',
        ]

        Will create Identifier, Relationship, Group and all M2M objects.
    """
    identifiers = sorted(set(sum([[a,b] for a, _, b in relationships],[])))
    groups = []  # Cointains pairs of (Identifier2Group, Group2Group)
    for i in identifiers:
        id_ = Identifier(value=i, scheme='doi')
        session.add(id_)
        groups.append(get_or_create_groups(session, id_))
    rel_obj = []
    id_gr_relationships = []
    ver_gr_relationships = []
    for src, rel, tar in relationships:
        src_, tar_ = Identifier.get(session, src, 'doi'), \
            Identifier.get(session, tar, 'doi')
        r = Relationship(source=src_, target=tar_, relation=rel)
        session.add(r)
        rel_obj.append(r)
        s_id_gr, s_ver_gr = groups[identifiers.index(src)]
        t_id_gr, t_ver_gr = groups[identifiers.index(tar)]
        id_gr_rel = GroupRelationship(source=s_id_gr,
            target=t_id_gr, relation=rel, type=GroupType.Identity)
        session.add(Relationship2GroupRelationship(relationship=r,
            group_relationship=id_gr_rel))
        session.add(id_gr_rel)
        ver_gr_rel = GroupRelationship(source=s_ver_gr,
            target=t_ver_gr, relation=rel, type=GroupType.Version)
        session.add(GroupRelationshipM2M(relationship=ver_gr_rel,
                                         subrelationship=id_gr_rel))
        session.add(ver_gr_rel)
    session.commit()


def get_group_from_id(session, identifier_value, id_type='doi',
                      group_type=GroupType.Identity):
    """Resolve from 'A' to Identity Group of A or to a Version Group of A."""
    id_ = Identifier.get(session, identifier_value, id_type)
    id_grp = id_.id2groups[0].group
    if group_type == GroupType.Identity:
        return id_grp
    else:
        return session.query(GroupM2M).filter_by(subgroup=id_grp).one().group


def assert_grouping(session, grouping):
    """Determine if database state corresponds to 'grouping' definition.

    See tests in test_grouping.py for example input.
    """
    groups, relationships, relationship_groups = grouping
    group_types = [
        (GroupType.Identity if isinstance(g[0], str) else GroupType.Version)
        for g in groups]

    # Mapping 'relationship_types' is a mapping between relationship index to:
    # * None if its a regular Relation between Identifiers
    # * GroupType.Identity if it's a relation between 'Identity'-type Groups
    # * GroupType.Version if it's a relation between 'Version'-type Groups
    relationship_types = [None if isinstance(r[0], str) else group_types[r[0]]
                          for r in relationships]

    id_groups = [g for g, t in zip(groups, group_types) if t == GroupType.Identity]
    uniqe_ids = set(sum(id_groups, []))

    # id_map is a mapping of str -> Identifier
    # E.g.: 'A' -> Instance('A', 'doi')
    id_map = dict(map(lambda x: (x, Identifier.get(session, x, 'doi')), uniqe_ids))

    group_map = []
    for g in groups:
        if isinstance(g[0], str):  # Identity group
            group_map.append(
                session.query(Identifier2Group).filter_by(
                    identifier=id_map[g[0]]).one().group)
        elif isinstance(g[0], int):  # GroupM2M
            group_map.append(
                session.query(GroupM2M).filter_by(
                    subgroup=group_map[g[0]]).one().group)

    rel_map = []
    for r in relationships:
        obj_a, relation, obj_b = r
        if isinstance(obj_a, str) and isinstance(obj_b, str):  # Identifiers relation
            rel_map.append(
                session.query(Relationship).filter_by(
                    source=id_map[obj_a], target=id_map[obj_b],
                    relation=relation).one()
            )
        elif isinstance(obj_a, int) and isinstance(obj_b, int):  # Groups relation
            rel_map.append(
                session.query(GroupRelationship).filter_by(
                    source=group_map[obj_a], target=group_map[obj_b],
                    relation=relation).one()
            )

    # Make sure all loaded identifiers are unique
    assert len(set(map(lambda x: x[1].id, id_map.items()))) == len(id_map)
    assert session.query(Identifier).count() == len(id_map)

    # Make sure there's correct number of Identitfier2Group records
    # and 'Identity'-type groups
    assert session.query(Identifier2Group).count() == len(id_map)
    assert session.query(Group).filter_by(
        type=GroupType.Identity).count() == len(id_groups)

    # Make sure that all loaded groups are unique
    assert len(set(map(lambda x: x.id, group_map))) == len(group_map)
    assert session.query(Group).count() == len(group_map)

    # Make sure there's correct number of GroupM2M records
    # and 'Version'-type groups
    m2m_groups = [g for g in groups if isinstance(g[0], int)]
    assert session.query(Group).filter_by(
        type=GroupType.Version).count() == len(m2m_groups)
    # There are as many M2M groups as there are Identity groups
    assert session.query(GroupM2M).count() == len(id_groups)

    # Make sure that all loaded relationships are unique
    id_rels = [r for r, t in zip(rel_map, relationship_types)
               if t is None]
    assert len(set(map(lambda x: x.id, id_rels))) == len(id_rels)
    assert session.query(Relationship).count() == len(id_rels)

    grp_rels = [r for r, t in zip(rel_map, relationship_types)
               if t is not None]
    # Make sure that all loaded groups relationships are unique
    assert len(set(map(lambda x: x.id, grp_rels))) == len(grp_rels)
    assert session.query(GroupRelationship).count() == len(grp_rels)

    # Make sure that GroupRelationshipM2M are correct
    id_grp_rels = [r for r, t in zip(rel_map, relationship_types)
                   if t == GroupType.Identity]
    # There are as many GroupRelationshipM2M objects as Identity Groups
    assert session.query(GroupRelationshipM2M).count() == len(id_grp_rels)

    # There are as many Relationship to GR items as Relationships
    n_rel2grrels = sum([len(x[1]) for x in relationship_groups
                        if isinstance(rel_map[x[1][0]], Relationship)])
    assert session.query(Relationship2GroupRelationship).count() == n_rel2grrels

    # Make sure that all GroupRelationshipM2M are matching
    for group_rel, group_subrels in relationship_groups:
        for group_subrel in group_subrels:
            if isinstance(rel_map[group_subrel], Relationship):
                assert session.query(Relationship2GroupRelationship).filter_by(
                    relationship=rel_map[group_subrel],
                    group_relationship=rel_map[group_rel]).one()
            else:  # isinstance(rel_map[group_rel], GroupRelationship):
                assert session.query(GroupRelationshipM2M).filter_by(
                    relationship=rel_map[group_rel],
                    subrelationship=rel_map[group_subrel]).one()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python gen.py relations_input.json')
        exit(1)
    with open(sys.argv[1], 'r') as fp:
        input_items = json.load(fp)
    res = generate_payloads(input_items)
    print(json.dumps(res, indent=2))
