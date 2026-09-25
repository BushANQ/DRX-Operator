import assert from 'node:assert/strict';
import { test } from 'node:test';
import { formatJsonText } from './codeText.ts';

test('formatting preserves original large numbers, exponents and repeated JSON fields', () => {
  const source = '{"id":9007199254740993,"id":9007199254740995,"time":1.2300e+4}';
  assert.equal(formatJsonText(source), '{\n  "id": 9007199254740993,\n  "id": 9007199254740995,\n  "time": 1.2300e+4\n}');
});

test('JSON indentation retains string escapes and does not reinterpret plain tool output', () => {
  const source = '{"body":"<html>\\n</html>","empty":[],"zero":0,"flag":false}';
  const formatted = formatJsonText(source);
  assert.ok(formatted?.includes('"<html>\\n</html>"'));
  assert.deepEqual(JSON.parse(formatted!), JSON.parse(source));
  assert.equal(formatJsonText('connection refused'), null);
  assert.equal(formatJsonText('null'), 'null');
});

test('very large or deeply nested output remains available without expanding indentation', () => {
  const nested = '['.repeat(80) + '0' + ']'.repeat(80);
  assert.equal(formatJsonText(nested), nested);
  const long = 'x'.repeat(50001);
  assert.equal(formatJsonText(long), long);
});
