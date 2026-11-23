import {heading} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {handoffStateLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

function stateBadge(state) {
  return `<span class="status" data-state="${esc(state)}">${esc(handoffStateLabels[state] || state)}</span>`;
}

export function handoffsPage(state) {
  const rows = state.handoffs.map(row=>[
    esc(row.asset),
    esc(row.seq),
    stateBadge(row.state),
    esc(row.handoff_no),
    esc(row.attempts),
    esc(date(row.dispatched_at)),
    esc(date(row.delivered_at)),
    esc(row.receipt_no || '—'),
  ]);
  const inflight = state.handoffs.filter(row=>row.state==='dispatched');
  return `${heading(topic.modules[4], '外包修复站按底片签字顺序领取，回执过期可重领，编号沿用')}
    <section class="brief">
      <h2>修复站领取</h2>
      <form id="poll-form" class="inline-form">
        <label>单次领取上限<input name="limit" type="number" min="1" max="50" value="5" required></label>
        <button type="submit">定时领取</button>
      </form>
      <p class="note">同一底片前一份未签收时会自动跳过，改发其他底片；停机期间积累的报告恢复后仍可领到。</p>
    </section>
    ${inflight.length ? `<section class="brief"><h2>回传签收回执</h2>
      <form id="receipt-form" class="inline-form">
        <label>交接编号<select name="dispatch_no">${inflight.map(x=>`<option value="${esc(x.handoff_no)}">${esc(x.asset)} #${esc(x.seq)} · ${esc(x.handoff_no)}</option>`).join('')}</select></label>
        <label>回执编号<input name="receipt_no" maxlength="64" required></label>
        <button type="submit">提交回执</button>
      </form><p class="note">过期回执会被作废；重复提交已生效回执不会重复签收。</p></section>` : ''}
    <h2>交接单</h2>
    ${records(['底片','顺序','状态','交接编号','尝试次数','派发时间','签收时间','回执编号'], rows, '暂无已签字报告的交接单。')}`;
}

export function bindHandoffs(api, update, fail) {
  submit('poll-form', async data=>{
    const result = await api('/repair/poll', {method:'POST', data:{limit:Number(data.get('limit'))}});
    const msg = document.getElementById('message');
    if (msg) { msg.textContent = `本次领取 ${result.count} 份。`; msg.className=''; }
    await update();
  }, fail);
  submit('receipt-form', async data=>{
    await api('/repair/receipt', {method:'POST', data:{dispatch_no:data.get('dispatch_no'), receipt_no:data.get('receipt_no')}});
    const msg = document.getElementById('message');
    if (msg) { msg.textContent = '回执已签收。'; msg.className=''; }
    await update();
  }, fail);
}
