/**
 * Contract and source guard tests for model maintenance settings.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const settingsDialog = read('components/SettingsDialog.tsx');
const modelSection = read('components/ModelMaintenanceSection.tsx');
const runtimeClient = read('runtime-client/SynapseRuntimeClient.ts');

test('SettingsDialog provides tabbed navigation with model maintenance', () => {
  assert.ok(settingsDialog.includes('role="tablist"'), 'Settings dialog must have a tablist');
  assert.ok(settingsDialog.includes('常规设置'), 'Settings dialog must have general tab');
  assert.ok(settingsDialog.includes('模型维护'), 'Settings dialog must have model maintenance tab');
  assert.ok(
    settingsDialog.includes('<ModelMaintenanceSection'),
    'Settings dialog must embed ModelMaintenanceSection',
  );
  assert.ok(
    modelSection.includes('toggleGroupCollapse'),
    'Model maintenance must support collapsible provider groups',
  );
  assert.ok(
    modelSection.includes('ChevronDown16Regular') && modelSection.includes('ChevronRight16Regular'),
    'Model maintenance must display collapse chevron indicators',
  );
});

test('ModelMaintenanceSection uses bundled Fluent icons and shared controls', () => {
  assert.ok(
    modelSection.includes("from '@fluentui/react-icons'"),
    'Model maintenance must use Fluent icons',
  );
  assert.ok(
    modelSection.includes('ui-button'),
    'Model maintenance must use shared ui-button styling',
  );
  assert.equal(
    /text-white|bg-white|border-white/.test(modelSection),
    false,
    'Model maintenance must not hardcode white colors',
  );
});

test('ModelMaintenanceSection uses dropdown provider and handles prefix automatically', () => {
  assert.ok(
    modelSection.includes('PROVIDER_METAS'),
    'Model maintenance must define provider metadata',
  );
  assert.ok(
    modelSection.includes('formData.provider'),
    'Model maintenance must bind provider to select element',
  );
  assert.ok(
    modelSection.includes('resolveGroupKey'),
    'Model maintenance must group models by provider',
  );
  assert.ok(
    modelSection.includes('<Portal>'),
    'Model maintenance must open edit/add form in a portalled modal dialog',
  );
});

test('SynapseRuntimeClient exposes all model maintenance RPC methods', () => {
  const methods = ['modelsList', 'modelsSave', 'modelsDelete', 'modelsSetDefault', 'modelsTest'];
  for (const method of methods) {
    assert.ok(
      runtimeClient.includes(`public async ${method}`),
      `SynapseRuntimeClient must declare ${method}`,
    );
  }
});
