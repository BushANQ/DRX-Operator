export function formatJsonText(source: string): string | null {
  try { JSON.parse(source); } catch { return null; }
  const tokens = source.match(/"(?:\\.|[^"\\])*"|[{}[\],:]|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g);
  if (!tokens) return null;
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
