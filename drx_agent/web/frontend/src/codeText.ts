export function formatJsonText(source: string): string | null {
  if (source.length > 50000) return source;
  try { JSON.parse(source); } catch { return null; }
  const tokens = source.match(/"(?:\\.|[^"\\])*"|[{}[\],:]|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g);
  if (!tokens) return null;
  let nesting = 0;
  for (const token of tokens) {
    if (token === '{' || token === '[') nesting += 1;
    if (token === '}' || token === ']') nesting -= 1;
    if (nesting > 64) return source;
  }
  const pieces: string[] = [];
  let depth = 0;
  const newline = () => pieces.push('\n', '  '.repeat(depth));
  tokens.forEach((token, index) => {
    if (token === '{' || token === '[') {
      pieces.push(token);
      const close = token === '{' ? '}' : ']';
      if (tokens[index + 1] !== close) { depth += 1; newline(); }
    } else if (token === '}' || token === ']') {
      const open = token === '}' ? '{' : '[';
      if (tokens[index - 1] !== open) { depth -= 1; newline(); }
      pieces.push(token);
    } else if (token === ',') { pieces.push(token); newline(); }
    else if (token === ':') pieces.push(': ');
    else pieces.push(token);
  });
  return pieces.join('');
}
