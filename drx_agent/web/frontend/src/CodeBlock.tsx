import { useMemo } from 'react';
import { formatJsonText } from './codeText.ts';

interface CodeBlockProps { value: string; output?: boolean }

export default function CodeBlock({ value, output = false }: CodeBlockProps) {
  const content = useMemo(() => {
    const formatted = formatJsonText(value);
    if (formatted === null) return value;
    if (formatted.length > 50000) return formatted;
    const expression = /"(?:\\.|[^"\\])*"\s*:|"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
    const parts: Array<{ text: string; tone: string }> = [];
    let cursor = 0;
    for (const match of formatted.matchAll(expression)) {
      const start = match.index;
      if (start > cursor) parts.push({ text: formatted.slice(cursor, start), tone: '' });
      const token = match[0];
      const tone = token.endsWith(':') ? 'key' : token.startsWith('"') ? 'string' : /^(true|false|null)$/.test(token) ? 'literal' : 'number';
      parts.push({ text: token, tone });
      cursor = start + token.length;
    }
    if (cursor < formatted.length) parts.push({ text: formatted.slice(cursor), tone: '' });
    return parts;
  }, [value]);
  return <pre className={`detail-code-pre ${output ? 'output-pre' : ''}`}><code>{typeof content === 'string' ? content : content.map((part, index) => <span className={part.tone ? `code-${part.tone}` : undefined} key={index}>{part.text}</span>)}</code></pre>;
}
