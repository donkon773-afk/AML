const { encode } = require("@toon-format/toon");
let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const data = JSON.parse(input);
  process.stdout.write(encode(data));
});
