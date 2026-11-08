#!/usr/bin/env python3
"""Builds the n8n workflow: AI customer-support e-mail agent grounded in a knowledge base."""
import os, sys
from wf_lib import Workflow, cond, conditions, switch_rules, nid

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'ai-support-agent.workflow.json')

PREPARE_JS = r'''// ===== CONFIG =====
const CONFIG = {
  ownDomains: ['yourcompany.com'],   // never answer your own team's e-mails
};

const from = ($json.from && $json.from.value && $json.from.value[0]) || {};
const address = String(from.address || '').toLowerCase();
const headers = JSON.stringify($json.headers || {}).toLowerCase();

// keep only the newest message, drop the quoted thread and signatures
const raw = String($json.text || $json.textPlain || '').replace(/\r/g, '');
const cutMarkers = [
  /^on .+wrote:\s*$/im, /^-{2,}\s*original message\s*-{2,}/im, /^from: .+$/im, /^sent from my /im,
  /^.+tarihinde .+yazdı:\s*$/im, /^am .+schrieb .+:\s*$/im, /^el .+escribió:\s*$/im, /^le .+a écrit\s*:\s*$/im,
];
let body = raw;
for (const re of cutMarkers) { const m = body.match(re); if (m && m.index > 0) body = body.slice(0, m.index); }
body = body.split('\n').filter(l => !l.trim().startsWith('>')).join('\n').replace(/\n{3,}/g, '\n\n').trim();

const skipReasons = [];
if (!address) skipReasons.push('no sender');
if (/(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?@|bounce)/i.test(address)) skipReasons.push('automated sender');
if (/auto-submitted: auto-|x-autoreply|x-autorespond|precedence: (bulk|list|junk)/i.test(headers)) skipReasons.push('auto-reply / bulk mail');
if (/list-unsubscribe/i.test(headers)) skipReasons.push('newsletter');
if (CONFIG.ownDomains.some(d => address.endsWith('@' + d))) skipReasons.push('internal e-mail');
if (!body) skipReasons.push('empty body');

return {
  json: {
    message_id: $json.id,
    thread_id: $json.threadId,
    received_at: new Date().toISOString(),
    customer_email: address,
    customer_name: from.name || '',
    customer_domain: address.split('@')[1] || '',
    subject: String($json.subject || '(no subject)'),
    body: body.slice(0, 6000),
    should_process: skipReasons.length === 0,
    skip_reason: skipReasons.join(', '),
  },
};
'''

RETRIEVE_JS = r'''// Finds the most relevant knowledge-base articles for each e-mail (BM25 keyword search – no vector DB needed).
const TOP_K = 4;
const STOP = new Set(('a an the and or but if to of in on at for from by with about as is are was were be been it this that ' +
  'i you we they he she my your our me us do does did can could would should will have has had not no yes hi hello ' +
  'thanks thank please regards dear ve ile bir bu şu da de mi mı için ama çok daha merhaba teşekkürler lütfen ben siz biz').split(' '));
const tokenize = s => String(s || '').toLowerCase().normalize('NFKC')
  .split(/[^\p{L}\p{N}]+/u).filter(t => t.length > 1 && !STOP.has(t));

const kb = $('Read Knowledge Base').all().map(i => i.json).filter(r => r && (r.question || r.answer));
const docs = kb.map((r, i) => {
  const tokens = [...tokenize(r.question), ...tokenize(r.question), ...tokenize(r.keywords), ...tokenize(r.keywords),
    ...tokenize(r.category), ...tokenize(r.answer)];
  const tf = {};
  for (const t of tokens) tf[t] = (tf[t] || 0) + 1;
  return { id: String(r.id || 'KB-' + (i + 1)), row: r, tf, len: tokens.length };
});
const N = docs.length;
const avgLen = N ? docs.reduce((a, d) => a + d.len, 0) / N : 1;
const df = {};
for (const d of docs) for (const t of Object.keys(d.tf)) df[t] = (df[t] || 0) + 1;
const k1 = 1.5, b = 0.75;
const bm25 = (qTokens, d) => {
  let s = 0;
  for (const t of new Set(qTokens)) {
    const f = d.tf[t];
    if (!f) continue;
    const idf = Math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5));
    s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * d.len / avgLen));
  }
  return s;
};

// iterate over the e-mails (the node's direct input is the knowledge-base rows)
return $('Skip Automated Mail').all().map((item, idx) => {
  const mail = item.json;
  const q = tokenize(mail.subject + ' ' + mail.body);
  const hits = docs.map(d => ({ d, score: bm25(q, d) }))
    .filter(h => h.score > 0).sort((x, y) => y.score - x.score).slice(0, TOP_K);
  const context = hits.map(h => [
    `[${h.d.id}] ${h.d.row.question || ''}`,
    String(h.d.row.answer || ''),
    h.d.row.url ? 'More info: ' + h.d.row.url : '',
  ].filter(Boolean).join('\n')).join('\n\n');
  return {
    json: {
      ...mail,
      kb_article_ids: hits.map(h => h.d.id),
      kb_top_score: hits.length ? Math.round(hits[0].score * 100) / 100 : 0,
      kb_context: context || '(no matching articles)',
    },
    pairedItem: { item: idx },
  };
});
'''

DECIDE_JS = r'''// ===== CONFIG – the support policy =====
const CONFIG = {
  autoSendEnabled: false,          // start in "draft mode"; switch on after a week of reviewing drafts
  autoSendCategories: ['order_status', 'shipping', 'product_question', 'account'],
  minConfidenceAutoSend: 0.9,
  alwaysEscalateCategories: ['complaint', 'partnership'],
  vipDomains: [],                  // e.g. ['bigclient.com'] – always escalated to a human
  telegramOnHighUrgency: true,
};

return $input.all().map((item, idx) => {
  const mail = $('Retrieve KB Articles').all()[idx].json;
  let ai;
  try {
    ai = JSON.parse(item.json.choices[0].message.content);
  } catch (e) {
    ai = { category: 'other', urgency: 'medium', sentiment: 'neutral', language: 'en', can_answer_from_kb: false,
      used_article_ids: [], confidence: 0, needs_human: true, needs_human_reason: 'AI response could not be parsed',
      reply_subject: '', reply_body: '', summary_for_agent: mail.body.slice(0, 300) };
  }

  const escalateWhy = [];
  if (ai.needs_human) escalateWhy.push(ai.needs_human_reason || 'AI asked for a human');
  if (!ai.can_answer_from_kb) escalateWhy.push('Not covered by the knowledge base');
  if (ai.sentiment === 'angry') escalateWhy.push('Angry customer');
  if (CONFIG.telegramOnHighUrgency && ai.urgency === 'high') escalateWhy.push('High urgency');
  if (CONFIG.alwaysEscalateCategories.includes(ai.category)) escalateWhy.push('Category: ' + ai.category);
  if (CONFIG.vipDomains.includes(mail.customer_domain)) escalateWhy.push('VIP customer');

  let action;
  if (ai.category === 'spam') action = 'ignore';
  else if (escalateWhy.length) action = 'escalate';
  else if (CONFIG.autoSendEnabled && CONFIG.autoSendCategories.includes(ai.category)
           && (Number(ai.confidence) || 0) >= CONFIG.minConfidenceAutoSend && ai.sentiment !== 'negative') action = 'auto_send';
  else action = 'draft';

  const subject = ai.reply_subject || (/^re:/i.test(mail.subject) ? mail.subject : 'Re: ' + mail.subject);
  const alert = [
    `🚨 Support e-mail needs a human (${ai.urgency} urgency, ${ai.sentiment})`,
    '',
    `From: ${mail.customer_name ? mail.customer_name + ' <' + mail.customer_email + '>' : mail.customer_email}`,
    `Subject: ${mail.subject}`,
    `Category: ${ai.category}`,
    '',
    `Summary: ${ai.summary_for_agent || ''}`,
    '',
    'Why escalated:',
    ...[...new Set(escalateWhy)].map(r => '• ' + r),
    '',
    '✍️ A suggested reply is in Gmail drafts – review, edit, send.',
  ].join('\n').slice(0, 3900);

  return {
    json: {
      ...mail,
      category: ai.category,
      urgency: ai.urgency,
      sentiment: ai.sentiment,
      language: ai.language,
      can_answer_from_kb: !!ai.can_answer_from_kb,
      used_article_ids: (ai.used_article_ids || []).join(', '),
      ai_confidence: Number(ai.confidence) || 0,
      summary_for_agent: ai.summary_for_agent || '',
      reply_subject: subject,
      reply_body: ai.reply_body || '',
      action,
      escalation_reasons: [...new Set(escalateWhy)].join(' | '),
      telegram_alert: alert,
    },
    pairedItem: { item: idx },
  };
});
'''

SHAPE_LOG_JS = r'''// One row per handled e-mail in the "Support Log" sheet (columns must match its header row).
const d = $('Decide Action').item.json;
const COLUMNS = ['received_at', 'customer_email', 'customer_name', 'subject', 'category', 'urgency', 'sentiment',
  'language', 'action', 'escalation_reasons', 'can_answer_from_kb', 'used_article_ids', 'ai_confidence',
  'summary_for_agent', 'reply_body', 'message_id', 'thread_id'];
const row = {};
for (const c of COLUMNS) row[c] = d[c] == null ? '' : d[c];
row.handled_at = new Date().toISOString();
return { json: row };
'''

OPENAI_BODY = r'''={{ (() => {
  const brand = $('⚙️ Support Settings').first().json;
  return JSON.stringify({
    model: 'gpt-4o-mini',
    temperature: 0.2,
    response_format: {
      type: 'json_schema',
      json_schema: {
        name: 'support_triage',
        strict: true,
        schema: {
          type: 'object',
          additionalProperties: false,
          required: ['category', 'urgency', 'sentiment', 'language', 'can_answer_from_kb', 'used_article_ids', 'confidence',
                     'needs_human', 'needs_human_reason', 'reply_subject', 'reply_body', 'summary_for_agent'],
          properties: {
            category: { type: 'string', enum: ['order_status', 'shipping', 'returns_refunds', 'billing', 'technical', 'account',
              'product_question', 'sales_inquiry', 'complaint', 'partnership', 'spam', 'other'] },
            urgency: { type: 'string', enum: ['high', 'medium', 'low'] },
            sentiment: { type: 'string', enum: ['positive', 'neutral', 'negative', 'angry'] },
            language: { type: 'string', description: 'ISO 639-1 code of the customer e-mail' },
            can_answer_from_kb: { type: 'boolean', description: 'true only if the knowledge base fully answers the question' },
            used_article_ids: { type: 'array', items: { type: 'string' } },
            confidence: { type: 'number', description: '0-1' },
            needs_human: { type: 'boolean' },
            needs_human_reason: { type: 'string' },
            reply_subject: { type: 'string' },
            reply_body: { type: 'string', description: 'Plain text reply to send to the customer' },
            summary_for_agent: { type: 'string', description: 'One sentence for the support team' }
          }
        }
      }
    },
    messages: [
      { role: 'system', content:
        'You are the customer support agent of ' + brand.company_name + '. ' + brand.company_description + '\n' +
        'Tone: ' + brand.tone + '\n\n' +
        'RULES:\n' +
        '1. Answer ONLY with facts from the KNOWLEDGE BASE below. Never invent order numbers, delivery dates, prices, refunds or policies.\n' +
        '2. If the knowledge base does not fully answer the question, set can_answer_from_kb=false and write a short, kind reply ' +
        'that confirms receipt, restates the request and says a team member will reply within ' + brand.response_time + '.\n' +
        '3. Never promise refunds, discounts or compensation; legal threats, chargebacks, safety issues or data-deletion requests -> needs_human=true.\n' +
        '4. Reply in the SAME language as the customer. Use the customer name if known. Max 160 words. No placeholders like [Order ID].\n' +
        '5. Sign as: ' + brand.signature + '\n\n' +
        'KNOWLEDGE BASE:\n' + $json.kb_context },
      { role: 'user', content: 'From: ' + ($json.customer_name || '') + ' <' + $json.customer_email + '>\nSubject: ' +
        $json.subject + '\n\n' + $json.body }
    ]
  });
})() }}'''

wf = Workflow("AI Customer Support Agent – Gmail → Knowledge Base → Reply / Draft / Escalate")
Y = 400

wf.node("Gmail: New Support E-mail", "n8n-nodes-base.gmailTrigger", 1.2, {
    "pollTimes": {"item": [{"mode": "everyMinute"}]},
    "simple": False,
    "filters": {"q": "in:inbox -category:promotions -category:social"},
    "options": {},
}, [0, Y])

wf.node("⚙️ Support Settings", "n8n-nodes-base.set", 3.4, {
    "mode": "manual",
    "assignments": {"assignments": [
        {"id": nid(), "name": "company_name", "type": "string", "value": "Acme Store"},
        {"id": nid(), "name": "company_description", "type": "string",
         "value": "We sell eco-friendly home products online and ship across Europe."},
        {"id": nid(), "name": "tone", "type": "string", "value": "Friendly, concise and professional. No emojis."},
        {"id": nid(), "name": "response_time", "type": "string", "value": "one business day"},
        {"id": nid(), "name": "signature", "type": "string", "value": "Acme Store Support Team"},
    ]},
    "includeOtherFields": True,
    "options": {},
}, [220, Y])

wf.node("Prepare E-mail", "n8n-nodes-base.code", 2, {"mode": "runOnceForEachItem", "jsCode": PREPARE_JS}, [440, Y])

wf.node("Skip Automated Mail", "n8n-nodes-base.filter", 2.2, {
    "conditions": conditions([cond("={{ $json.should_process }}", "boolean", "true", single=True)]),
    "options": {},
}, [660, Y])

wf.node("Read Knowledge Base", "n8n-nodes-base.googleSheets", 4.5, {
    "operation": "read",
    "documentId": {"__rl": True, "mode": "id", "value": "YOUR_GOOGLE_SHEET_ID"},
    "sheetName": {"__rl": True, "mode": "name", "value": "Knowledge Base"},
    "options": {},
}, [880, Y], executeOnce=True, alwaysOutputData=True)

wf.node("Retrieve KB Articles", "n8n-nodes-base.code", 2, {"jsCode": RETRIEVE_JS}, [1100, Y])

wf.node("AI: Triage & Draft Answer", "n8n-nodes-base.httpRequest", 4.2, {
    "method": "POST",
    "url": "https://api.openai.com/v1/chat/completions",
    "authentication": "predefinedCredentialType",
    "nodeCredentialType": "openAiApi",
    "sendBody": True,
    "specifyBody": "json",
    "jsonBody": OPENAI_BODY,
    "options": {"timeout": 90000},
}, [1320, Y], retryOnFail=True, maxTries=3, waitBetweenTries=3000)

wf.node("Decide Action", "n8n-nodes-base.code", 2, {"jsCode": DECIDE_JS}, [1540, Y])

wf.node("Route", "n8n-nodes-base.switch", 3.2, switch_rules([
    ("auto_send", cond("={{ $json.action }}", "string", "equals", "auto_send")),
    ("draft", cond("={{ $json.action }}", "string", "equals", "draft")),
    ("escalate", cond("={{ $json.action }}", "string", "equals", "escalate")),
]), [1760, Y])

wf.node("Gmail: Send Reply", "n8n-nodes-base.gmail", 2.1, {
    "operation": "reply",
    "messageId": "={{ $('Decide Action').item.json.message_id }}",
    "emailType": "text",
    "message": "={{ $('Decide Action').item.json.reply_body }}",
    "options": {"appendAttribution": False},
}, [2000, Y - 200], webhookId=nid())

wf.node("Gmail: Save Reply as Draft", "n8n-nodes-base.gmail", 2.1, {
    "resource": "draft",
    "subject": "={{ $('Decide Action').item.json.reply_subject }}",
    "emailType": "text",
    "message": "={{ $('Decide Action').item.json.reply_body }}",
    "options": {
        "threadId": "={{ $('Decide Action').item.json.thread_id }}",
        "sendTo": "={{ $('Decide Action').item.json.customer_email }}",
    },
}, [2000, Y], webhookId=nid())

wf.node("Telegram: Alert Support Team", "n8n-nodes-base.telegram", 1.2, {
    "chatId": "YOUR_TELEGRAM_CHAT_ID",
    "text": "={{ $('Decide Action').item.json.telegram_alert }}",
    "additionalFields": {"appendAttribution": False},
}, [2000, Y + 200], webhookId=nid())

wf.node("Shape Log Row", "n8n-nodes-base.code", 2, {"mode": "runOnceForEachItem", "jsCode": SHAPE_LOG_JS}, [2240, Y - 100])

wf.node("Append to Support Log", "n8n-nodes-base.googleSheets", 4.5, {
    "operation": "append",
    "documentId": {"__rl": True, "mode": "id", "value": "YOUR_GOOGLE_SHEET_ID"},
    "sheetName": {"__rl": True, "mode": "name", "value": "Support Log"},
    "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": [], "schema": []},
    "options": {},
}, [2460, Y - 100])

wf.sticky("""## 💬 AI Customer Support Agent
Reads every new support e-mail, finds the right answers in **your** knowledge base and writes the reply in the customer's language.

✅ Simple, covered questions → reply sent automatically *(off by default – starts in draft mode)*
✍️ Everything else → ready-to-send draft in the same Gmail thread
🚨 Angry / urgent / VIP / not covered → Telegram alert to the team + draft
📊 Every e-mail logged to Google Sheets (category, sentiment, action)""", [-40, Y - 470], 720, 290, 4)

wf.sticky("""### ⚙️ Setup (15 min)
1. Connect Gmail (trigger + 2 Gmail nodes), OpenAI, Google Sheets, Telegram.
2. Google Sheet with two tabs: **Knowledge Base** (`id, category, question, answer, keywords, url`) and **Support Log** (header row in README). A sample knowledge base is included.
3. Edit **⚙️ Support Settings** (brand, tone, signature) and the domains in **Prepare E-mail**.
4. Review drafts for a week, then set `autoSendEnabled: true` in **Decide Action**.""", [720, Y - 470], 700, 290, 6)

wf.sticky("""### 🛡 Guardrails
The AI may only use knowledge-base facts, never promises refunds or dates, and hands legal threats, chargebacks and data requests to a human. Newsletters, auto-replies, no-reply senders and internal mail are skipped.""", [1460, Y - 420], 520, 200, 5)

for a, b, i in [
    ("Gmail: New Support E-mail", "⚙️ Support Settings", 0),
    ("⚙️ Support Settings", "Prepare E-mail", 0),
    ("Prepare E-mail", "Skip Automated Mail", 0),
    ("Skip Automated Mail", "Read Knowledge Base", 0),
    ("Read Knowledge Base", "Retrieve KB Articles", 0),
    ("Retrieve KB Articles", "AI: Triage & Draft Answer", 0),
    ("AI: Triage & Draft Answer", "Decide Action", 0),
    ("Decide Action", "Route", 0),
    ("Route", "Gmail: Send Reply", 0),
    ("Route", "Gmail: Save Reply as Draft", 1),
    ("Route", "Gmail: Save Reply as Draft", 2),
    ("Route", "Telegram: Alert Support Team", 2),
    ("Gmail: Send Reply", "Shape Log Row", 0),
    ("Gmail: Save Reply as Draft", "Shape Log Row", 0),
    ("Shape Log Row", "Append to Support Log", 0),
]:
    wf.link(a, b, i)

wf.save(OUT)
