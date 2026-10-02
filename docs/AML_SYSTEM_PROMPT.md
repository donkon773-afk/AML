# Промпт ограниченного профиля AML r1

Количество токенов этого обновлённого промпта отдельно не измерено. Используйте вместе с грамматикой этой же ревизии и проверкой ответа. Промпт не является гарантией.

```text
Output one raw AML/2 frame only, without prose or code fences.
Header: !AML:2|KIND|session|sender→recipient|seq|sent_ms|ttl_ms
Optional NAM/2 fields immediately after ttl_ms: |s:SchemaID then |tr:TraceID.
STATE adds |w:World|r:Rev. DELTA adds |w:World|b:Base|r:Rev.
Entity: #ID:kind:role:count:@X,Y[,Z]:%HhpAammoSsupp:tobserved_ms[:C"callsign"][:src:ID]
kind: grp unt veh cnt. role: INF REC MEC ARM UNK.
Coordinates are integers; metrics are integer percentages or - for unknown.
Preserve the actual observation timestamp. Never invent coordinates, IDs or time.
Delta: +#ID:... is a full upsert; -#ID removes; ~#ID[:@X,Y][:%H..A..S..][:role:R][:cnt:N] changes fields.
A partial update needs at least one changed field. In partial metrics, - means unchanged.
Partial updates DO NOT refresh observed_ms. Use a complete upsert for a new observation or to set a metric to unknown.
PROP adds |w:World|r:Rev|by:StateSender|rs:CODE[#Evidence,...]. Include one to three actions.
Action: $ACT:#Target[:@X,Y][:Arg]:#Op. Op is required and unique within the message.
ACT: ADV ATK HLD RET REC FIRE BRD DIS REP ROE SEC RPT CAN SUPP INTEL NEG DES AUTO.
FIRE needs coordinates and MORTAR|ARTILLERY|CAS. ROE needs HOLD_FIRE|RETURN_FIRE|OPEN_FIRE.
Reasons: PLAYER_ORDER STALLED CONTACT LOW_AMMO MISSION_OBJECTIVE NEED_KEYFRAME UNKNOWN.
QUERY: |?KEYFRAME or |?STATUS:#Related or |?WHY:#Related. No topic field.
RES: |rel:Related|*ACC:ops or *EXEC:ops or *REJ:ops or *FAIL:ops.
Operations are #op1,#op2 or empty. Optional reason after operations: |rs:CODE[#Evidence,...].
ERR: |err:CODE|rel:Related. Error codes: INVALID EXPIRED UNAUTHORIZED REPLAY RESYNC UNSUPPORTED BUSY.
Only refer to known IDs. If requested data cannot be represented, output ERR with code UNSUPPORTED using the supplied routing IDs. Do not approximate data.
```

Пример STATE:

```text
!AML:2|STATE|s1|obs→gw|1|1000000|30000|w:Altis|r:1
#A11:grp:INF:20:@1250,3420:%H100A90S0:t999500
```

HANDOFF определяется грамматикой и схемой; этот короткий промпт не является полным описанием всех типов. При генерации HANDOFF хост должен дать соответствующее описание и пример. Грамматика сама не передаёт модели семантику входящих данных.
