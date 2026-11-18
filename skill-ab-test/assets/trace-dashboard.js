(() => {
  const root = document.getElementById('skill-ab-dashboard');
  if (!root) return;
  const all = (selector, within = root) => Array.from(within.querySelectorAll(selector));
  const byId = id => {
    const element = document.getElementById(id);
    return element && root.contains(element) ? element : null;
  };
  const selectPanel = name => {
    if (!byId(`panel-${name}`)) return;
    all('.dashboard-panel').forEach(panel => { panel.hidden = panel.id !== `panel-${name}`; });
    all('.dashboard-tabs [data-panel]').forEach(button => {
      const selected = button.dataset.panel === name;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
  };
  const selectAction = (agent, id) => {
    if (!agent || !all('.action-detail', agent).some(detail => detail.dataset.eventId === id)) return;
    all('.action-detail', agent).forEach(detail => { detail.hidden = detail.dataset.eventId !== id; });
    all('.action-button', agent).forEach(button => {
      const selected = button.dataset.eventTarget === id;
      button.setAttribute('aria-pressed', String(selected));
      if (selected) {
        const auxiliary = button.closest('details.aux-events');
        if (auxiliary) auxiliary.open = true;
      }
    });
  };
  const selectVerdict = id => {
    if (!all('.verdict-detail').some(detail => detail.id === id)) return;
    all('.verdict-detail').forEach(detail => { detail.hidden = detail.id !== id; });
    all('.verdict-button').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.verdictTarget === id));
    });
  };
  const selectAnswer = name => {
    if (!all('.answer-case').some(section => section.dataset.answerCase === name)) return;
    all('.answer-case').forEach(section => { section.hidden = section.dataset.answerCase !== name; });
    all('[data-answer-select]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.answerSelect === name));
    });
  };
  const selectInspector = (kind, id) => {
    if (!all(`.${kind}-detail`).some(detail => detail.id === id)) return;
    all(`.${kind}-detail`).forEach(detail => { detail.hidden = detail.id !== id; });
    all(`.${kind}-button`).forEach(button => {
      button.setAttribute('aria-pressed', String(button.getAttribute(`data-${kind}-target`) === id));
    });
  };
  const reveal = (id, scroll = true) => {
    const target = byId(id);
    if (!target) return;
    const panel = target.closest('.dashboard-panel');
    if (panel) selectPanel(panel.id.slice('panel-'.length));
    const action = target.closest('.action-detail');
    if (action) selectAction(action.closest('.agent-panel'), action.dataset.eventId);
    const verdict = target.closest('.verdict-detail');
    if (verdict) selectVerdict(verdict.id);
    const answer = target.closest('.answer-case');
    if (answer) selectAnswer(answer.dataset.answerCase);
    for (const kind of ['file', 'flow']) {
      const inspector = target.closest(`.${kind}-detail`);
      if (inspector) selectInspector(kind, inspector.id);
    }
    for (let node = target; node && root.contains(node); node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
    if (scroll) target.scrollIntoView({block: 'start', behavior: 'auto'});
  };
  const revealHash = () => {
    try { reveal(decodeURIComponent(location.hash.slice(1))); } catch { /* Invalid fragment stays inert. */ }
  };
  root.addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const tab = event.target.closest('.dashboard-tabs [data-panel]');
    if (tab) { selectPanel(tab.dataset.panel); return; }
    const action = event.target.closest('.action-button');
    if (action) { selectAction(action.closest('.agent-panel'), action.dataset.eventTarget); return; }
    const verdict = event.target.closest('.verdict-button');
    if (verdict) { selectVerdict(verdict.dataset.verdictTarget); return; }
    const answer = event.target.closest('[data-answer-select]');
    if (answer) { selectAnswer(answer.dataset.answerSelect); return; }
    for (const kind of ['file', 'flow']) {
      const control = event.target.closest(`.${kind}-button`);
      if (control) { selectInspector(kind, control.getAttribute(`data-${kind}-target`)); return; }
    }
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    let id;
    try { id = decodeURIComponent(link.getAttribute('href').slice(1)); } catch { return; }
    if (!byId(id)) return;
    event.preventDefault();
    reveal(id);
    try { history.replaceState(null, '', `#${encodeURIComponent(id)}`); } catch { /* Sandboxed previews may disallow history. */ }
  });
  root.addEventListener('keydown', event => {
    if (!(event.target instanceof Element)) return;
    const tab = event.target.closest('.dashboard-tabs [data-panel]');
    if (!tab || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = all('.dashboard-tabs [data-panel]');
    const index = tabs.indexOf(tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    selectPanel(tabs[next].dataset.panel);
    tabs[next].focus();
  });
  let printState = [];
  let printDetails = [];
  let printAnswers = [];
  addEventListener('beforeprint', () => {
    printState = all('.dashboard-panel, .action-detail, .verdict-detail, .file-detail, .flow-detail').map(element => [element, element.hidden]);
    printDetails = all('details').map(element => [element, element.open]);
    printAnswers = all('.answer-case').map(element => [element, element.hidden]);
    printState.forEach(([element]) => { element.hidden = false; });
    printDetails.forEach(([element]) => { element.open = true; });
    printAnswers.forEach(([element]) => { element.hidden = element.dataset.answerCase !== 'all'; });
  });
  addEventListener('afterprint', () => {
    printState.forEach(([element, hidden]) => { element.hidden = hidden; });
    printDetails.forEach(([element, open]) => { element.open = open; });
    printAnswers.forEach(([element, hidden]) => { element.hidden = hidden; });
  });
  addEventListener('hashchange', revealHash);
  revealHash();
})();
