// Minimal VS Code client for the Pa language server (`pac lsp`).
// Inlay hints (inferred types / memory strategy / .paw FFI bridges), hover memory-audit cards and
// diagnostics are all served by the server; this file only launches and restarts it.
const vscode = require('vscode');
const path = require('path');
const fs = require('fs');
const { LanguageClient } = require('vscode-languageclient/node');

let client;

function resolvePac() {
  const configured = vscode.workspace.getConfiguration('pa').get('pacPath');
  if (configured && configured !== 'pac') return configured;
  for (const folder of vscode.workspace.workspaceFolders || []) {
    for (const rel of ['.venv/bin/pac', 'venv/bin/pac', '.venv/Scripts/pac.exe']) {
      const candidate = path.join(folder.uri.fsPath, rel);
      if (fs.existsSync(candidate)) return candidate;
    }
  }
  return 'pac';
}

async function startClient() {
  const command = resolvePac();
  client = new LanguageClient(
    'pac',
    'Pa Language Server',
    { command, args: ['lsp'] },
    { documentSelector: [{ language: 'pa' }, { language: 'paw' }] }
  );
  try {
    await client.start();
  } catch (err) {
    vscode.window.showErrorMessage(
      `Pa: could not start "${command} lsp" (${err.message}). Install with \`pip install -e .\` and/or set "pa.pacPath".`
    );
  }
}

function runInTerminal(args) {
  const editor = vscode.window.activeTextEditor;
  if (!editor || !['pa', 'paw'].includes(editor.document.languageId)) {
    vscode.window.showWarningMessage('Pa: open a .pa file first.');
    return;
  }
  editor.document.save().then(() => {
    const term = vscode.window.createTerminal('pac');
    term.show();
    term.sendText(`"${resolvePac()}" ${args.join(' ')} "${editor.document.fileName}"`);
  });
}

async function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('pa.restartServer', async () => {
      if (client) await client.stop();
      await startClient();
    }),
    vscode.commands.registerCommand('pa.buildDev', () => runInTerminal(['build', '--mode', 'dev'])),
    vscode.commands.registerCommand('pa.buildProd', () => runInTerminal(['build', '--mode', 'prod'])),
    vscode.commands.registerCommand('pa.check', () => runInTerminal(['check', '--ai-diagnostic']))
  );
  await startClient();
}

async function deactivate() {
  if (client) await client.stop();
}

module.exports = { activate, deactivate, resolvePac };
