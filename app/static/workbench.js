(function () {
  var state = { payload: null, kind: 'bars', run: null, approved: false };
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
    });
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function setNotice(text) { $('notice').textContent = text || ''; }
  function renderIssues(data) {
    $('issue-count').textContent = data.issues.length + ' 条';
    $('issue-list').innerHTML = data.issues.length ? data.issues.map(function (item) {
      return '<li><span>' + esc(item.line || '-') + '</span><span>' + esc(item.key || '无主键') + '</span><code>' + esc(item.code) + '</code></li>';
    }).join('') : '<li class="empty">暂无异常</li>';
  }
  function renderBreakdown(items) {
    if (!items || !items.length) return '';
    return '<details><summary>' + items.length + ' 项</summary><ul>' + items.map(function (item) {
      return '<li>' + esc(item) + '</li>';
    }).join('') + '</ul></details>';
  }
  function renderRows(data) {
    var labels = Object.assign({label: '记录'}, state.resultColumns);
    var source = data.row_kind === 'source';
    var hasBreakdown = data.rows.some(function (row) { return row.breakdown && row.breakdown.length; });
    var keys = source ? state.inputColumns : Object.keys(labels).filter(function (key) {
      return data.rows.some(function (row) { return Object.hasOwn(row, key); });
    });
    $('row-count').textContent = source ? data.rows.length + ' 行预览' :
      data.summary.PASS + ' 通过 / ' + data.summary.REVIEW + ' 复核 / ' + data.summary.REJECT + ' 阻断';
    var tail = source ? '' : '<th>结论</th>' + (hasBreakdown ? '<th>计算分项</th>' : '');
    $('row-head').innerHTML = '<tr>' + keys.map(function (key) { return '<th>' + esc(labels[key] || key) + '</th>'; }).join('') + tail + '</tr>';
    $('row-body').innerHTML = data.rows.length ? data.rows.map(function (row) {
      var values = keys.map(function (key) { return '<td class="' + (typeof row[key] === 'number' ? 'num' : '') + '">' + esc(row[key]) + '</td>'; }).join('');
      var verdict = source ? '' : '<td><span class="verdict" data-v="' + esc(row.verdict) + '">' + esc(row.verdict || '') + '</span><br><small>' + esc(row.reason || '') + '</small></td>';
      var breakdown = hasBreakdown ? '<td>' + renderBreakdown(row.breakdown) + '</td>' : '';
      return '<tr>' + values + verdict + breakdown + '</tr>';
    }).join('') : '<tr><td class="empty" colspan="' + (keys.length + (source ? 0 : 1)) + '">没有可展示条目</td></tr>';
  }
  function renderLedger(data) {
    if (!data.ledger) { $('ledger-list').innerHTML = '<p class="empty">运行对账后显示</p>'; return; }
    var values = data.ledger;
    $('ledger-list').innerHTML = [
      ['来源行', values.counts.input], ['采信行', values.counts.accepted], ['隔离行', values.counts.isolated],
      ['精确重复', values.counts.duplicates], ['忽略行', values.counts.ignored], ['计算条目', values.counts.computed]
    ].map(function (row) { return '<div class="ledger-row"><span>' + row[0] + '</span><strong>' + row[1] + '</strong></div>'; }).join('') +
      '<div class="ledger-row"><span>' + esc(values.metric_field) + '</span><strong>' + esc(values.metric_total) + '</strong></div>' +
      values.checks.map(function (check) { return '<div class="ledger-row"><span>' + esc(check.label) + '</span><strong class="' + (check.ok ? 'check-ok' : 'check-bad') + '">' + (check.ok ? '通过' : '异常') + '</strong></div>'; }).join('');
  }
  function render(data) {
    state.payload = data;
    $('chart-label').textContent = Object.values(state.resultColumns)[0];
    $('m-input').textContent = data.counts.input;
    $('m-accepted').textContent = data.counts.accepted;
    $('m-isolated').textContent = data.counts.isolated + data.counts.duplicates;
    $('m-rejected').textContent = data.summary.REJECT;
    renderRows(data);
    renderIssues(data);
    renderLedger(data);
    window.renderChart($('chart'), data.chart && data.chart.length ? (state.kind || 'bars') : 'bars', data.chart || []);
  }
  function stageButtons(stages) {
    $('stage-nav').innerHTML = stages.map(function (stage, index) {
      return '<button type="button" data-stage="' + stage.id + '" aria-pressed="' + (index === 0 ? 'true' : 'false') + '">' + esc(stage.name) + '</button>';
    }).join('');
    Array.from(document.querySelectorAll('[data-stage]')).forEach(function (button) {
      button.addEventListener('click', function () {
        document.querySelectorAll('[data-stage]').forEach(function (other) { other.setAttribute('aria-pressed', String(other === button)); });
        loadStage(button.getAttribute('data-stage'));
      });
    });
  }
  async function loadStage(stage) {
    try {
      setNotice('');
      document.body.setAttribute('aria-busy', 'true');
      var payload = await request('/api/stages/' + stage, {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      render(payload);
    } catch (error) { setNotice(error.message); }
    finally { document.body.removeAttribute('aria-busy'); }
  }
  async function runBatch() {
    try {
      setNotice('');
      $('run').disabled = true;
      var run = await request('/api/runs', {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      state.run = run;
      state.approved = run.status === 'APPROVED';
      render(run);
      renderOrderBoard(run);
      $('run-status').textContent = run.status + ' · rev ' + run.revision;
      $('revision').textContent = 'rev ' + run.revision;
      refreshGlobalActions(run);
      $('download').hidden = !state.approved;
      if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
    } catch (error) { setNotice(error.message); }
    finally { $('run').disabled = false; }
  }
  async function review(decision) {
    if (!state.run) return;
    try {
      var run = await request('/api/runs/' + state.run.run_id + '/review', {
        method: 'POST',
        body: JSON.stringify({decision: decision, comment: $('comment').value, expected_revision: state.run.revision})
      });
      state.run = run;
      state.approved = run.status === 'APPROVED';
      renderOrderBoard(run);
      $('run-status').textContent = run.status + ' · rev ' + run.revision;
      $('revision').textContent = 'rev ' + run.revision;
      $('sign').disabled = true;
      $('hold').disabled = true;
      if ($('issue-ready')) $('issue-ready').disabled = true;
      $('download').hidden = !state.approved;
      if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
    } catch (error) { setNotice(error.message); }
  }
  var STATUS_LABELS = {READY: '可签发', HELD: '暂缓', BLOCKED: '阻断', ISSUED: '已签发', OPEN: '未整批暂缓'};
  var REASON_LABELS = {
    INVALID_LINE_AMOUNT: '行金额无法解析', INVALID_DISCOUNT_TOTAL: '优惠字段无效',
    INVALID_RETURN_QTY: '退货数量无效', INVALID_QTY: '数量无效', INVALID_UNIT_REFUND: '单价无效',
    INVALID_FREIGHT: '运费无效', INVALID_PAYMENT: '支付金额无效',
    BAD_RETURN_QTY: '退货数量超过购买数量', BAD_AMOUNT: '金额为负', BAD_POSITIVE: '数量必须为正',
    COLUMN_COUNT: '列数不符', CONFLICT_DUPLICATE: '同一商品行内容冲突/挂到多张订单',
    KEY_MISSING: '商品行主键为空', BATCH_HOLD: '存在无法归属的坏行，整批暂缓',
    MANUAL_HOLD: '人工暂缓', OVER_REFUND: '应退超过实付', ORDER_FIELDS_CONFLICT: '订单级字段不一致'
  };
  function reasonText(code) { return REASON_LABELS[code] || code; }
  function fmtAmount(value) {
    if (value === null || value === undefined) return '金额待补正';
    var n = Number(value);
    return (n / 100).toFixed(2) + ' 元';
  }
  function refreshGlobalActions(run) {
    if (!run) {
      $('sign').disabled = true; $('hold').disabled = true;
      if ($('issue-ready')) $('issue-ready').disabled = true;
      return;
    }
    var board = run.review_board;
    var hasUnresolved = board && board.orders.some(function (o) {
      return o.status === 'HELD' || o.status === 'BLOCKED';
    });
    var readyCount = board ? board.orders.filter(function (o) { return o.status === 'READY'; }).length : 0;
    $('sign').disabled = run.summary.REJECT > 0 || state.approved ||
      !!(board && board.batch_hold) || hasUnresolved;
    $('hold').disabled = state.approved;
    if ($('issue-ready')) $('issue-ready').disabled = readyCount === 0;
  }
  function applyRun(run) {
    state.run = run;
    state.approved = run.status === 'APPROVED';
    render(run);
    renderOrderBoard(run);
    $('run-status').textContent = run.status + ' · rev ' + run.revision;
    $('revision').textContent = 'rev ' + run.revision;
    refreshGlobalActions(run);
    $('download').hidden = !state.approved;
    if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
  }
  function correctionTemplate(run, order) {
    var bad = (order.bad_rows && order.bad_rows[0]) || null;
    var targetLine = bad ? bad.line : null;
    var record = null;
    if (targetLine !== null && run.records) {
      record = run.records.find(function (item) { return item.line === targetLine; }) || null;
    }
    var values = {};
    if (record && record.cells) {
      state.inputColumns.forEach(function (col, idx) {
        if (idx < record.cells.length) values[col] = record.cells[idx];
      });
    } else if (bad && bad.key) {
      values.line_id = bad.key;
    }
    return JSON.stringify({line: targetLine, values: values}, null, 2);
  }
  function renderOrderBoard(run) {
    var boardEl = $('order-board');
    var banner = $('batch-hold-banner');
    if (!boardEl) return;
    if (!run || !run.review_board) {
      boardEl.innerHTML = '<p class="empty">执行批次后，按订单显示暂缓、补正与签发状态。</p>';
      if (banner) banner.hidden = true;
      return;
    }
    var board = run.review_board;
    if (banner) {
      banner.hidden = !board.batch_hold;
      banner.textContent = board.batch_hold
        ? '有坏行认不出归属，整批暂缓；请先补正下列无归属坏行（来源行 ' +
          board.unattributable.map(function (u) { return u.line || '-'; }).join('、') + '）。'
        : '';
    }
    boardEl.innerHTML = board.orders.map(function (order) {
      var status = order.status;
      var amountLine = order.issued
        ? '<span class="amt issued-amt">已签发 ' + esc(fmtAmount(order.issued_amount)) + '</span>'
        : '<span class="amt">待签 ' + esc(fmtAmount(order.pending_amount)) +
          ' <small>v' + esc(order.amount_version) + '</small></span>';
      var stale = order.invalidated_amount !== null && order.invalidated_amount !== undefined
        ? '<p class="stale-note">旧待签金额 ' + esc(fmtAmount(order.invalidated_amount)) +
          ' 已失效，请按 v' + esc(order.amount_version) + ' 重算金额签发。</p>' : '';
      var reasons = (order.reasons || []).length
        ? '<div class="reason-chips">' + order.reasons.map(function (code) {
            return '<code title="' + esc(code) + '">' + esc(reasonText(code)) + '</code>';
          }).join('') + '</div>' : '';
      var voucher = order.voucher
        ? '<p class="voucher">凭证 <a href="/api/runs/' + esc(run.run_id) + '/vouchers/' +
          encodeURIComponent(order.order_id) + '" target="_blank" rel="noopener">' +
          esc(order.voucher.voucher_id) + '</a> · 金额与凭证已冻结，重试不会重复签发</p>' : '';
      var badList = (order.bad_rows || []).length
        ? '<ul class="bad-lines">' + order.bad_rows.map(function (b) {
            return '<li>来源行 ' + esc(b.line || '-') + ' · <code>' + esc(b.code) +
              '</code>' + (b.order_ids && b.order_ids.length > 1
                ? ' · 同时挂到 ' + esc(b.order_ids.join(' / ')) : '') + '</li>';
          }).join('') + '</ul>' : '';
      var correction = status === 'ISSUED' ? '' :
        '<details class="correct"><summary>补正此单坏行</summary>' +
        '<textarea class="correction-json" rows="7" spellcheck="false">' +
          esc(correctionTemplate(run, order)) + '</textarea>' +
        '<div class="correct-actions">' +
          '<button type="button" data-act="correct">提交补正</button>' +
          '<button type="button" data-act="correct-issue" class="primary">补正后签发</button>' +
        '</div></details>';
      var actions = status === 'ISSUED' ? '' :
        '<div class="order-actions">' +
          (status === 'READY' ? '<button type="button" class="primary" data-act="issue">签发此单</button>' : '') +
          (status === 'HELD' ? '<button type="button" data-act="retry">重试签发</button>' : '') +
          '<button type="button" data-act="hold">暂缓此单</button>' +
        '</div>';
      return '<div class="order-card" data-order="' + esc(order.order_id) + '" data-status="' + esc(status) + '">' +
        '<div class="order-head"><strong>' + esc(order.order_id) + '</strong>' +
        '<span class="verdict" data-v="' + esc(status === 'BLOCKED' ? 'REJECT' : status) + '">' +
          esc(STATUS_LABELS[status] || status) + '</span>' + amountLine + '</div>' +
        stale + reasons + badList + voucher + correction + actions +
        '<p class="action-error" hidden></p></div>';
    }).join('');
  }
  async function postOrderAction(card, pathSuffix, body) {
    if (!state.run) return;
    var orderId = card.getAttribute('data-order');
    var errorEl = card.querySelector('.action-error');
    if (errorEl) { errorEl.hidden = true; errorEl.textContent = ''; }
    document.body.setAttribute('aria-busy', 'true');
    try {
      var data = await request('/api/runs/' + state.run.run_id + '/orders/' +
        encodeURIComponent(orderId) + pathSuffix, {method: 'POST', body: JSON.stringify(body || {})});
      applyRun(data.run || data);
      setNotice('订单 ' + orderId + ' 操作完成：' + (data.order_status || data.status || ''));
    } catch (error) {
      if (errorEl) { errorEl.hidden = false; errorEl.textContent = error.message; }
      setNotice(error.message);
      // Refresh board in case the server state moved (e.g. stale revision).
      try { applyRun(await request('/api/runs/' + state.run.run_id)); } catch (_) {}
    } finally {
      document.body.removeAttribute('aria-busy');
    }
  }
  async function issueReady() {
    if (!state.run) return;
    try {
      document.body.setAttribute('aria-busy', 'true');
      var data = await request('/api/runs/' + state.run.run_id + '/issue-all', {
        method: 'POST', body: JSON.stringify({comment: $('comment').value})
      });
      applyRun(data.run || data);
      setNotice('已签发 ' + (data.issued_orders || []).length + ' 张可签订单；暂缓/阻断订单保持不变。');
    } catch (error) {
      setNotice(error.message);
    } finally {
      document.body.removeAttribute('aria-busy');
    }
  }
  function bootOrderBoard() {
    var boardEl = $('order-board');
    if (!boardEl || boardEl.dataset.bound) return;
    boardEl.dataset.bound = '1';
    boardEl.addEventListener('click', function (event) {
      var button = event.target.closest('[data-act]');
      if (!button) return;
      var card = button.closest('.order-card');
      if (!card) return;
      var act = button.getAttribute('data-act');
      if (act === 'issue') {
        postOrderAction(card, '/issue', {comment: $('comment').value});
      } else if (act === 'hold') {
        postOrderAction(card, '/hold', {comment: $('comment').value});
      } else if (act === 'retry') {
        postOrderAction(card, '/retry', {comment: $('comment').value});
      } else if (act === 'correct' || act === 'correct-issue') {
        var textarea = card.querySelector('.correction-json');
        var parsed;
        try { parsed = JSON.parse(textarea.value); }
        catch (error) { setNotice('补正内容不是合法 JSON：' + error.message); return; }
        if (act === 'correct-issue') parsed.and_issue = true;
        parsed.comment = $('comment').value;
        postOrderAction(card, '/correct', parsed);
      }
    });
  }
  async function boot() {
    try {
      var catalog = await request('/api/catalog');
      state.kind = catalog.chart;
      state.resultColumns = catalog.result_columns;
      state.inputColumns = catalog.columns;
      stageButtons(catalog.stages.slice(0, 5));
      document.querySelector('.rail-note').textContent = catalog.code + ' · ' + catalog.stages[6].name;
      await loadStage('intake');
    } catch (error) { setNotice(error.message); }
    $('run').addEventListener('click', runBatch);
    $('sign').addEventListener('click', function () { review('APPROVED'); });
    $('hold').addEventListener('click', function () { review('HELD'); });
    var issueReadyButton = $('issue-ready');
    if (issueReadyButton) issueReadyButton.addEventListener('click', issueReady);
    bootOrderBoard();
    $('profile').addEventListener('change', function () {
      state.run = null; state.approved = false;
      $('sign').disabled = true; $('hold').disabled = true;
      if ($('issue-ready')) $('issue-ready').disabled = true;
      $('download').hidden = true;
      $('run-status').textContent = '未执行批次'; $('revision').textContent = 'rev 0';
      renderOrderBoard(null);
      loadStage('intake');
    });
  }
  document.addEventListener('DOMContentLoaded', boot);
})();
