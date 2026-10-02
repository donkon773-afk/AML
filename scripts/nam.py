"""NAM/1 reference validator and bounded in-memory shadow receiver. No executor."""
import argparse
import copy
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / 'assets' / 'nam.schema.json').read_text(encoding='utf-8'))
MAX_BYTES = 65536
# Cluster profile (T37): which entity kind each cluster action may target.
CLUSTER_TARGETS = {'REASSIGN': 'task', 'RETURN': 'task', 'APPROVE': 'task',
                   'PAUSE': 'agent', 'RESUME': 'agent'}


class ProtocolError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ProtocolError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ProtocolError('non-finite JSON value: ' + value)


def _depth(value, level=0):
    require(level <= 32, 'message too deep')
    if isinstance(value, dict):
        for key, child in value.items():
            require(key not in ('__proto__', 'constructor', 'prototype'), 'unsafe key')
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)
    elif isinstance(value, str):
        require(not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value), 'control or surrogate character')
    elif isinstance(value, float):
        require(math.isfinite(value), 'non-finite number')


def _type(value, expected):
    return {'object': lambda: isinstance(value, dict),
            'array': lambda: isinstance(value, list),
            'string': lambda: isinstance(value, str),
            'integer': lambda: type(value) is int,
            'number': lambda: type(value) in (int, float),
            'null': lambda: value is None}[expected]()


def _check(value, schema):
    """Only the explicitly used keywords in the pinned bundled schema."""
    if '$ref' in schema:
        ref = schema['$ref']
        require(ref.startswith('#/$defs/'), 'unsupported reference')
        _check(value, SCHEMA['$defs'][ref.split('/')[-1]])
    if 'type' in schema:
        kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        require(any(_type(value, k) for k in kinds), 'wrong value type')
    if 'const' in schema:
        require(value == schema['const'], 'unsupported constant')
    if 'not' in schema:
        try:
            _check(value, schema['not'])
        except ProtocolError:
            pass
        else:
            raise ProtocolError('forbidden field for this protocol version')
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                _check(value, option)
            except ProtocolError:
                continue
            break
        else:
            raise ProtocolError('no matching alternative')
    if 'enum' in schema:
        require(value in schema['enum'], 'unsupported enum')
    if isinstance(value, dict):
        require(len(value) >= schema.get('minProperties', 0), 'too few fields')
        props = schema.get('properties', {})
        require(all(k in value for k in schema.get('required', [])), 'missing required field')
        if schema.get('additionalProperties') is False:
            require(set(value) <= set(props), 'unknown field')
        for key in value.keys() & props.keys():
            _check(value[key], props[key])
    if isinstance(value, str):
        require(len(value) >= schema.get('minLength', 0) and len(value) <= schema.get('maxLength', MAX_BYTES), 'string length')
        if 'pattern' in schema:
            require(re.search(schema['pattern'], value) is not None, 'string pattern')
    if type(value) in (int, float):
        require(schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf), 'number range')
    if isinstance(value, list):
        require(schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', MAX_BYTES), 'array length')
        if schema.get('uniqueItems'):
            require(len({json.dumps(x, sort_keys=True) for x in value}) == len(value), 'duplicate array item')
        for child in value:
            _check(child, schema.get('items', {}))
    for child in schema.get('allOf', []):
        _check(value, child)
    if 'if' in schema:
        try:
            _check(value, schema['if'])
        except ProtocolError:
            _check(value, schema.get('else', {}))
        else:
            _check(value, schema.get('then', {}))


def decode(raw):
    try:
        if isinstance(raw, bytes):
            require(len(raw) <= MAX_BYTES, 'message too large')
            raw = raw.decode('utf-8', errors='strict')
        require(isinstance(raw, str), 'expected JSON text')
        require(len(raw.encode('utf-8')) <= MAX_BYTES, 'message too large')
        message = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        _depth(message)
        _check(message, SCHEMA)
        return message
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ProtocolError('invalid JSON encoding/structure') from exc


def encode(message):
    raw = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    decode(raw)
    return raw


def entity_map(entities, now_ms, age_ms):
    result = {}
    for entity in entities:
        require(entity['id'] not in result, 'duplicate entity ID')
        require(now_ms - age_ms <= entity['observed_ms'] <= now_ms + 2000, 'stale/future observation')
        result[entity['id']] = copy.deepcopy(entity)
    return result


class Receiver:
    """Trusted host authenticates peers; this class cannot execute any action."""
    def __init__(self, session, recipient, permissions, state_age_ms=30000, schemas=None):
        require(0 < len(permissions) <= 64, 'peer count')
        self.session, self.recipient = session, recipient
        self.permissions = {k: frozenset(v) for k, v in permissions.items()}
        self.state_age_ms = state_age_ms
        # NAM/2: payload-schema IDs this host understands. Empty set means the
        # host supports no payload schema; advertising an unknown ID fails.
        self.schemas = frozenset(schemas or ())
        self.agreed = {}
        self.sequences, self.states, self.seen = {}, {}, {}

    def accept(self, raw, authenticated_peer, now_ms):
        message = decode(raw)
        peer, kind, body = message['sender'], message['kind'], message['body']
        require(peer == authenticated_peer, 'peer identity mismatch')
        require(kind in self.permissions.get(peer, ()), 'unauthorized kind')
        require(message['session'] == self.session and message['recipient'] == self.recipient, 'wrong session/recipient')
        expires = message['sent_ms'] + message['ttl_ms']
        require(message['sent_ms'] <= now_ms + 2000 and now_ms < expires, 'expired/future frame')
        require(message['seq'] > self.sequences.get(peer, -1), 'replay/out-of-order')
        live_seen = {k: v for k, v in self.seen.items() if v > now_ms}
        require((peer, message['id']) not in live_seen, 'duplicate message ID')
        require(len(live_seen) < 4096, 'receiver busy')
        # NAM/2 payload-schema binding. A schema ID only carries meaning once
        # both sides have advertised it in hello; otherwise the recipient would
        # be trusting a number the sender invented.
        if 'schema' in message:
            require(kind == 'hello' or message['schema'] in self.agreed.get(peer, frozenset()),
                    'unnegotiated payload schema')
        updated = None
        if kind == 'hello':
            offered = frozenset(body.get('schemas', ()))
            require('NAM/2' in body['versions'] or not offered, 'schemas require NAM/2')
            common = offered & self.schemas
            require(not offered or common, 'no common payload schema')
            self.agreed[peer] = common
        elif kind == 'state':
            old = self.states.get(peer)
            require(old is None or (body['world'] == old['world'] and body['rev'] > old['rev']), 'state rollback/world mismatch')
            updated = {'world': body['world'], 'rev': body['rev'], 'entities': entity_map(body['entities'], now_ms, self.state_age_ms)}
        elif kind == 'delta':
            old = self.states.get(peer)
            require(old is not None and body['world'] == old['world'] and body['base'] == old['rev'] and body['rev'] == body['base'] + 1, 'RESYNC')
            additions = entity_map(body['upsert'], now_ms, self.state_age_ms)
            partials = body.get('updates', [])
            partial_ids = [p['id'] for p in partials]
            require(len(set(partial_ids)) == len(partial_ids), 'duplicate partial update target')
            require(not set(additions).intersection(body['remove']), 'ambiguous delta')
            require(not set(partial_ids).intersection(body['remove']), 'ambiguous delta')
            require(not set(partial_ids).intersection(additions), 'ambiguous delta')
            require(set(body['remove']) <= old['entities'].keys(), 'unknown removal')
            require(set(partial_ids) <= old['entities'].keys(), 'unknown partial-update target')
            updated = copy.deepcopy(old)
            for key in body['remove']:
                del updated['entities'][key]
            for partial in partials:
                target = updated['entities'][partial['id']]
                for field in ('pos', 'hp', 'ammo', 'suppression', 'role', 'count'):
                    if field in partial:
                        target[field] = partial[field]
                # Deliberately keep observed_ms. Full upsert is required to
                # refresh an observation; changing one field proves no such thing.
            updated['entities'].update(additions)
            updated['rev'] = body['rev']
            require(len(updated['entities']) <= 128, 'state capacity')
        elif kind == 'proposal':
            state = self.states.get(body['state_sender'])
            require(state is not None and body['world'] == state['world'] and body['rev'] == state['rev'], 'proposal needs current keyframe')
            entities = state['entities']
            operations = [a['op'] for a in body['actions']]
            require(len(set(operations)) == len(operations), 'duplicate operation')
            for action in body['actions']:
                target = entities.get(action['target'])
                allowed = (CLUSTER_TARGETS[action['type']],) if action['type'] in CLUSTER_TARGETS else ('group', 'unit', 'vehicle')
                require(target is not None and target['kind'] in allowed, 'invalid action target')
                if action['type'] == 'REASSIGN':
                    agent = entities.get(action['to'])
                    require(agent is not None and agent['kind'] == 'agent' and agent['state'] in ('active', 'stale'),
                            'reassign to unavailable agent')
                    require(action['to'] != target['critic'], 'executor cannot be the critic')
                require(now_ms - target['observed_ms'] <= self.state_age_ms, 'stale target')
                if 'position' in action:
                    require(any(now_ms - e['observed_ms'] <= self.state_age_ms and math.dist(action['position'][:2], e['pos'][:2]) <= 250 for e in entities.values()), 'ungrounded position')
            for identity in body['reason']['evidence']:
                require(identity in entities and now_ms - entities[identity]['observed_ms'] <= self.state_age_ms, 'missing/stale evidence')
        elif kind == 'handoff':
            paths = []
            for artifact in body['artifacts']:
                path = artifact['path']
                require(not path.startswith('/') and '\\' not in path and ':' not in path,
                        'artifact must have a portable relative path')
                require(all(part not in ('', '.', '..') for part in path.split('/')), 'unsafe artifact path')
                paths.append(path)
            require(len(paths) == len(set(paths)), 'duplicate artifact path')
            # Hashes are sender claims. Do not open files or execute next/summary.
        # Commit only after every validation succeeds. Returned data is detached.
        if updated is not None:
            self.states[peer] = updated
        self.sequences[peer] = message['seq']
        live_seen[(peer, message['id'])] = expires
        self.seen = live_seen
        return copy.deepcopy(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate'])
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    try:
        message = decode(args.file.read_bytes())
    except (ProtocolError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps({'valid': True, 'kind': message['kind'], 'id': message['id'], 'scope': 'syntax only; no execution'}))


if __name__ == '__main__':
    main()
