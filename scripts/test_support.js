// Runs the support workflow's code nodes and the OpenAI body expression with mock n8n globals.
const fs = require('fs');
const assert = require('assert');
const path = process.argv[2] || require('path').join(__dirname, '..', 'ai-support-agent.workflow.json');
const kbPath = process.argv[3] || require('path').join(__dirname, '..', 'sample-knowledge-base.csv');
const wf = JSON.parse(fs.readFileSync(path, 'utf8'));
const byName = Object.fromEntries(wf.nodes.map(n => [n.name, n]));
const code = name => byName[name].parameters.jsCode;
const run = (name, ctx) => new Function('$', '$json', '$input', code(name))(ctx.$, ctx.$json, ctx.$input);

for (const [from, c] of Object.entries(wf.connections)) {
  assert.ok(byName[from], from);
  for (const outs of c.main) for (const t of outs) assert.ok(byName[t.node], t.node);
}
for (const r of JSON.stringify(wf.nodes).match(/\$\('([^']+)'\)/g) || []) assert.ok(byName[r.slice(3, -2)], r);
console.log('✓ all connections and $() references point to real nodes');

// ---------- 1) Prepare E-mail
const gmail = (over = {}) => ({
  id: 'msg1', threadId: 'thr1', subject: 'Where is my order?',
  from: { value: [{ address: 'Anna.Schmidt@web.de', name: 'Anna Schmidt' }], text: 'Anna Schmidt <anna.schmidt@web.de>' },
  headers: { from: 'From: Anna Schmidt <anna.schmidt@web.de>' },
  text: 'Hi, I ordered a bamboo toothbrush set 5 days ago and it has not arrived yet. How long does shipping to Germany take?\n\nThanks, Anna\n\nOn Mon, 2 Nov 2026 at 10:00, Acme Store <support@yourcompany.com> wrote:\n> Thanks for your order\n> blah',
  company_name: 'Acme Store', ...over });
let p = run('Prepare E-mail', { $json: gmail() });
assert.equal(p.json.customer_email, 'anna.schmidt@web.de');
assert.equal(p.json.customer_name, 'Anna Schmidt');
assert.equal(p.json.should_process, true);
assert.ok(p.json.body.endsWith('Thanks, Anna'), JSON.stringify(p.json.body));
assert.ok(!p.json.body.includes('Thanks for your order'));
const anna = p.json;
p = run('Prepare E-mail', { $json: gmail({ text: 'Merhaba, siparişim nerede?\n\n2 Kas 2026 Pzt 10:00 tarihinde Acme <a@b.com> şunu yazdı:\n> eski' }) });
assert.ok(!p.json.body.includes('eski'));
for (const [over, why] of [
  [{ from: { value: [{ address: 'no-reply@shop.com' }] } }, 'automated sender'],
  [{ headers: { 'auto-submitted': 'Auto-Submitted: auto-replied' } }, 'auto-reply'],
  [{ headers: { 'list-unsubscribe': 'List-Unsubscribe: <mailto:x>' } }, 'newsletter'],
  [{ from: { value: [{ address: 'ceo@yourcompany.com' }] } }, 'internal'],
  [{ text: '> only quoted' }, 'empty body'],
]) {
  const r = run('Prepare E-mail', { $json: gmail(over) });
  assert.equal(r.json.should_process, false, why);
  assert.ok(r.json.skip_reason.includes(why), r.json.skip_reason);
}
console.log('✓ Prepare E-mail strips quoted threads (EN/TR) and skips automated, bulk, internal and empty mail');

// ---------- 2) Retrieve KB Articles with the sample knowledge base
const parseCsv = text => {
  const rows = []; let row = [], cur = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"' && text[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; }
    else if (c === '"') q = true;
    else if (c === ',') { row.push(cur); cur = ''; }
    else if (c === '\n') { row.push(cur); rows.push(row); row = []; cur = ''; }
    else if (c !== '\r') cur += c;
  }
  if (cur || row.length) { row.push(cur); rows.push(row); }
  const [h, ...rest] = rows;
  return rest.filter(r => r.length > 1).map(r => Object.fromEntries(h.map((k, i) => [k, r[i] || ''])));
};
const kb = parseCsv(fs.readFileSync(kbPath, 'utf8'));
assert.ok(kb.length >= 10, 'sample KB has ' + kb.length + ' rows');
const retrieve = (mails, rows) => run('Retrieve KB Articles', {
  $: name => name === 'Read Knowledge Base' ? { all: () => rows.map(j => ({ json: j })) } : { all: () => mails.map(j => ({ json: j })) },
  $input: { all: () => rows.map(j => ({ json: j })) },
});
let [k] = retrieve([anna], kb);
const shippingId = kb.find(r => /how long does shipping/i.test(r.question)).id;
assert.equal(k.json.kb_article_ids[0], shippingId, k.json.kb_article_ids.join(','));
assert.ok(k.json.kb_context.includes('[' + shippingId + ']'));
const refundMail = { ...anna, subject: 'Refund', body: 'I want to return the glass bottles and get my money back. What is your return policy?' };
[k] = retrieve([refundMail], kb);
assert.ok(/return/i.test(kb.find(r => r.id === k.json.kb_article_ids[0]).question), k.json.kb_article_ids.join(','));
const trMail = { ...anna, subject: 'Kargo', body: 'Siparişim ne zaman kargoya verilir? Kargo takip numarası nerede?' };
[k] = retrieve([trMail], kb);
assert.ok(k.json.kb_article_ids.length > 0, 'turkish query finds articles');
[k] = retrieve([anna], [{}]);
assert.deepEqual(k.json.kb_article_ids, []);
assert.equal(k.json.kb_context, '(no matching articles)');
const many = retrieve([anna, refundMail], kb);
assert.deepEqual(many.map(x => x.pairedItem.item), [0, 1]);
assert.notEqual(many[0].json.kb_article_ids[0], many[1].json.kb_article_ids[0]);
const withKb = retrieve([anna], kb)[0].json;
console.log('✓ Retrieve KB Articles ranks the right article first (shipping, returns, Turkish) and survives an empty KB');

// ---------- 3) OpenAI body
const inner = byName['AI: Triage & Draft Answer'].parameters.jsonBody.replace(/^=\{\{/, '').replace(/\}\}$/, '');
const settings = { company_name: 'Acme Store', company_description: 'Eco products.', tone: 'Friendly', response_time: 'one business day', signature: 'Acme Support' };
const body = JSON.parse(new Function('$', '$json', 'return ' + inner)(() => ({ first: () => ({ json: settings }) }), withKb));
const sch = body.response_format.json_schema.schema;
assert.equal(body.response_format.json_schema.strict, true);
assert.deepEqual(Object.keys(sch.properties).sort(), [...sch.required].sort());
assert.ok(body.messages[0].content.includes('[' + shippingId + ']'));
assert.ok(body.messages[0].content.includes('Acme Support'));
assert.ok(body.messages[1].content.includes('anna.schmidt@web.de'));
console.log('✓ OpenAI body is valid, strict schema consistent, KB context and brand injected');

// ---------- 4) Decide Action
const ai = { category: 'shipping', urgency: 'low', sentiment: 'neutral', language: 'en', can_answer_from_kb: true,
  used_article_ids: [shippingId], confidence: 0.95, needs_human: false, needs_human_reason: '',
  reply_subject: 'Re: Where is my order?', reply_body: 'Hi Anna, ...', summary_for_agent: 'Asks about shipping time to DE.' };
const decide = (aiObjs, mails, autoSend = false) => {
  let src = code('Decide Action');
  if (autoSend) src = src.replace('autoSendEnabled: false', 'autoSendEnabled: true');
  return new Function('$', '$input', src)(
    () => ({ all: () => mails.map(j => ({ json: j })) }),
    { all: () => aiObjs.map(o => ({ json: { choices: [{ message: { content: typeof o === 'string' ? o : JSON.stringify(o) } }] } })) });
};
assert.equal(decide([ai], [withKb])[0].json.action, 'draft');                 // draft mode by default
assert.equal(decide([ai], [withKb], true)[0].json.action, 'auto_send');       // auto-send once enabled
assert.equal(decide([{ ...ai, confidence: 0.7 }], [withKb], true)[0].json.action, 'draft');
assert.equal(decide([{ ...ai, category: 'returns_refunds' }], [withKb], true)[0].json.action, 'draft');
let e = decide([{ ...ai, sentiment: 'angry' }], [withKb], true)[0].json;
assert.equal(e.action, 'escalate');
assert.ok(e.telegram_alert.includes('Angry customer') && e.telegram_alert.includes('anna.schmidt@web.de'));
assert.equal(decide([{ ...ai, can_answer_from_kb: false }], [withKb])[0].json.action, 'escalate');
assert.equal(decide([{ ...ai, category: 'complaint' }], [withKb])[0].json.action, 'escalate');
assert.equal(decide([{ ...ai, needs_human: true, needs_human_reason: 'Legal threat' }], [withKb])[0].json.escalation_reasons.includes('Legal threat'), true);
assert.equal(decide([{ ...ai, category: 'spam' }], [withKb])[0].json.action, 'ignore');
e = decide(['not json'], [withKb])[0].json;
assert.equal(e.action, 'escalate');
assert.equal(e.reply_subject, 'Re: Where is my order?');
const d2 = decide([ai, { ...ai, urgency: 'high' }], [withKb, withKb]);
assert.deepEqual(d2.map(x => x.json.action), ['draft', 'escalate']);
assert.deepEqual(d2.map(x => x.pairedItem.item), [0, 1]);
console.log('✓ Decide Action: draft mode, auto-send gate, escalation rules, spam, broken AI output');

// ---------- 5) Shape Log Row + routing
const decided = decide([ai], [withKb])[0].json;
const row = run('Shape Log Row', { $: () => ({ item: { json: decided } }) }).json;
assert.equal(Object.keys(row).length, 18);
assert.ok(!('telegram_alert' in row) && !('kb_context' in row) && !('body' in row));
assert.deepEqual(byName['Route'].parameters.rules.values.map(v => v.outputKey), ['auto_send', 'draft', 'escalate']);
assert.deepEqual(wf.connections['Route'].main.map(o => o.map(t => t.node)),
  [['Gmail: Send Reply'], ['Gmail: Save Reply as Draft'], ['Gmail: Save Reply as Draft', 'Telegram: Alert Support Team']]);
console.log('✓ log row has 18 columns; routing auto_send -> reply, draft -> draft, escalate -> draft + Telegram');
console.log('\nALL TESTS PASSED');
