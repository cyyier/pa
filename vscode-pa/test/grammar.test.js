// Tokenizes a sample with the real TextMate engine (vscode-textmate + oniguruma).
// `source.python` is provided by VS Code itself at run time, so it resolves to nothing here;
// the Pa-specific scopes are what we verify.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vsctm = require('vscode-textmate');
const oniguruma = require('vscode-oniguruma');

async function makeRegistry() {
  const wasm = fs.readFileSync(path.join(__dirname, '../node_modules/vscode-oniguruma/release/onig.wasm')).buffer;
  await oniguruma.loadWASM(wasm);
  return new vsctm.Registry({
    onigLib: Promise.resolve({
      createOnigScanner: (p) => new oniguruma.OnigScanner(p),
      createOnigString: (s) => new oniguruma.OnigString(s),
    }),
    loadGrammar: async (scope) => {
      const file = { 'source.pa': 'pa', 'source.paw': 'paw' }[scope];
      if (!file) return null;
      const p = path.join(__dirname, `../syntaxes/${file}.tmLanguage.json`);
      return vsctm.parseRawGrammar(fs.readFileSync(p, 'utf8'), p);
    },
  });
}

function scopesOf(grammar, line, text) {
  const { tokens } = grammar.tokenizeLine(line, vsctm.INITIAL);
  return tokens.filter((t) => line.slice(t.startIndex, t.endIndex) === text).map((t) => t.scopes);
}

test('manifest registers pa and paw languages, grammars and colours', () => {
  const pkg = require('../package.json');
  assert.deepStrictEqual(pkg.contributes.languages.map((l) => l.id), ['pa', 'paw']);
  assert.deepStrictEqual(pkg.contributes.grammars.map((g) => g.scopeName), ['source.pa', 'source.paw']);
  const scopes = JSON.stringify(pkg.contributes.configurationDefaults['editor.tokenColorCustomizations']);
  for (const s of [
    'source.pa entity.name.function.python',
    'source.pa meta.function-call.generic.python',
    'source.paw entity.name.function.python',
    'support.function.intrinsic.pa',
    'entity.name.function.decorator.pa',
    'support.type.primitive.pa',
  ]) {
    assert.ok(scopes.includes(s), s);
  }
  for (const f of ['syntaxes/pa.tmLanguage.json', 'syntaxes/paw.tmLanguage.json', 'language-configuration.json']) {
    assert.doesNotThrow(() => JSON.parse(fs.readFileSync(path.join(__dirname, '..', f), 'utf8')), f);
  }
});

test('intrinsics, decorators and types get Pa scopes', async () => {
  const g = await (await makeRegistry()).loadGrammar('source.pa');
  const flat = (line, text) => scopesOf(g, line, text).flat();
  for (const fn of ['mut', 'span', 'copy', 'alloc', 'free']) {
    assert.ok(flat(`x = ${fn}(a, 1)`, fn).includes('support.function.intrinsic.pa'), fn);
  }
  assert.ok(flat('@struct', 'struct').includes('entity.name.function.decorator.pa'));
  assert.ok(flat('@export', 'export').includes('entity.name.function.decorator.pa'));
  assert.ok(flat('@allow_nan', 'allow_nan').includes('entity.name.function.decorator.pa'));
  assert.ok(flat('    x: f64', 'f64').includes('support.type.primitive.pa'));
  assert.ok(flat('def f(a: span[f64]) -> Vec[u8]:', 'Vec').includes('support.type.generic.pa'));
  assert.ok(flat('def f(a: span[f64]) -> Vec[u8]:', 'span').includes('support.type.generic.pa'));
});

test('span as a call is an intrinsic, span[...] is a type, plain names are untouched', async () => {
  const g = await (await makeRegistry()).loadGrammar('source.pa');
  assert.ok(!scopesOf(g, 'span = 3', 'span').flat().includes('support.function.intrinsic.pa'));
  assert.ok(!scopesOf(g, 'x = spanner(1)', 'spanner').flat().includes('support.function.intrinsic.pa'));
  assert.ok(scopesOf(g, 'y = span(a, 0, 2)', 'span').flat().includes('support.function.intrinsic.pa'));
});

test('paw grammar inherits the pa grammar', async () => {
  const g = await (await makeRegistry()).loadGrammar('source.paw');
  assert.ok(scopesOf(g, 'def f(a: span[f64]) -> f64:', 'f64').flat().includes('support.type.primitive.pa'));
});
