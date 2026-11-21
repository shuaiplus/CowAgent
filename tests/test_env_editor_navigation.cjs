// Run with Node: test the real view scripts with an in-memory DOM and fake API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const script = file => fs.readFileSync(path.join(root, 'channel/web/static/js', file), 'utf8');

class Classes {
    constructor() { this.items = new Set(); }
    add(c) { this.items.add(c); }
    remove(c) { this.items.delete(c); }
    contains(c) { return this.items.has(c); }
    toggle(c, force = !this.contains(c)) { force ? this.add(c) : this.remove(c); return force; }
}
class Element {
    constructor() {
        this.classList = new Classes(); this.dataset = {}; this.value = '';
        this.type = 'text'; this.firstElementChild = { className: '' }; this.style = {};
        this.children = []; this.attributes = {};
    }
    setAttribute(k, v) { this.attributes[k] = v; }
    append(...els) { this.children.push(...els); }
    replaceChildren() { this.children = []; }
    addEventListener() {}
    scrollIntoView() {}
    focus() {}
    reset() { get('env-key').value = ''; get('env-value').value = ''; }
}
const elements = {};
const get = id => elements[id] ||= new Element();
for (const name of ['editor', 'current', 'feedback', 'model-hint']) get('env-' + name).classList.add('hidden');
const listeners = {};
const document = {
    hidden: false, getElementById: get, createElement: () => new Element(),
    querySelectorAll: () => [],
    addEventListener: (event, callback) => (listeners[event] ||= []).push(callback),
};
const listing = { status: 'success', variables: [{key: 'TEST_KEY', preview: '***'}] };
let pendingConfirm;
const context = vm.createContext({
    document, window: {innerWidth: 1200}, I18N: {zh: {}, 'zh-Hant': {}, en: {}},
    t: key => key, setTimeout: () => 0, clearTimeout: () => {},
    fetch: async () => ({status: 200, ok: true, json: async () => listing}),
    menuSyncActive() {}, closeAgentDetail() {}, stopDevicesView() {}, loadModelsView() {},
    loadIntegrationsView() {}, loadDevicesView() {}, loadConfigView() {}, routeNoteTab() {},
    showConfirmDialog: opts => { pendingConfirm = opts.onConfirm; },
});
vm.runInContext(script('views/environment.js'), context);
const config = script('views/config.js');
vm.runInContext(config.slice(config.indexOf('function switchConfigTab(')), context);
const nav = script('core/nav.js');
vm.runInContext(nav.slice(nav.indexOf('const VIEW_META'), nav.indexOf('function toggleSidebar()')), context);
const call = code => vm.runInContext(code, context);
const checkDraft = (key, value) => {
    assert.equal(get('env-editor').classList.contains('hidden'), false);
    assert.equal(get('env-key').value, key);
    assert.equal(get('env-value').value, value);
};

(async () => {
    call("openEnvEditor({key: 'TEST_KEY', preview: '***'})");
    get('env-value').value = 'dummy-secret';
    document.hidden = true;
    for (const listener of listeners.visibilitychange || []) listener();
    document.hidden = false;
    for (const listener of listeners.visibilitychange || []) listener();
    checkDraft('TEST_KEY', 'dummy-secret');
    call("switchConfigTab('models'); switchConfigTab('environment')");
    await new Promise(resolve => setImmediate(resolve));
    checkDraft('TEST_KEY', 'dummy-secret');
    call("_switchToView('skills'); _switchToView('config'); switchConfigTab('environment')");
    await new Promise(resolve => setImmediate(resolve));
    checkDraft('TEST_KEY', 'dummy-secret');
    call('clearEnvEditor()');
    assert.equal(get('env-editor').classList.contains('hidden'), true);
    assert.equal(get('env-value').value, '');
    call('openEnvEditor()');
    get('env-key').value = 'NEW_TEST_KEY'; get('env-value').value = 'another-dummy';
    call("switchConfigTab('basic'); switchConfigTab('environment')");
    await new Promise(resolve => setImmediate(resolve));
    checkDraft('NEW_TEST_KEY', 'another-dummy');
    await call('saveEnvVariable({preventDefault() {}})');
    assert.equal(get('env-editor').classList.contains('hidden'), true);
    assert.equal(get('env-value').value, '');
    call("openEnvEditor({key: 'TEST_KEY', preview: '***'}); deleteEnvVariable({key: 'TEST_KEY'})");
    get('env-value').value = 'dummy-delete';
    await pendingConfirm();
    assert.equal(get('env-editor').classList.contains('hidden'), true);
    assert.equal(get('env-value').value, '');
    console.log('Environment editor navigation regression checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
