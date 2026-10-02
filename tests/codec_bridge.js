// JSON-lines-free batch bridge used by Python cross-language regression tests.
const fs = require('node:fs');
const { AMLCodec } = require('../scripts/aml_codec.js');
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(requests.map(({op, value}) => {
  try {
    if (op === 'decode') return {ok:true, message:AMLCodec.decode(value)};
    const wire = AMLCodec.encode(value);
    return {ok:true, wire, message:AMLCodec.decode(wire)};
  } catch (e) { return {ok:false, error:e.message}; }
})));
