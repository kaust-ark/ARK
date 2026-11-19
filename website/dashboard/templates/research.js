// Research milestones come from the durable project journal; transient token and log
// traffic never changes the scientific state or triggers a screen-reader announcement.
const researchLabels = {
  question_conclusion: ['Question the judgment on page %s','质疑第 %s 页的判断','ناقش الحكم على الصفحة %s'],
  question_result: ['Question this result on page %s','质疑第 %s 页的这条结果','ناقش هذه النتيجة في الصفحة %s'],
  question_explanation: ['Question this explanation on page %s','质疑第 %s 页的这条解释','ناقش هذا التفسير في الصفحة %s'],
  claim_result: ['Reported result','论文报告的结果','النتيجة المذكورة في الورقة'],
  claim_explanation: ['Proposed explanation','论文提出的解释','التفسير المقترح في الورقة'],
  claim_basis: ['Reviewer’s basis: %s','评审依据：%s','أساس حكم المراجع: %s'],
  basis_measurement: ['measurement','实测数据','القياس'],
  basis_control: ['control comparison','对照比较','المقارنة الضابطة'],
  basis_derivation: ['derivation','推导','الاشتقاق'],
  basis_qualified: ['qualified wording','作者的限定措辞','صياغة المؤلف المشروطة'],
  basis_unsupported: ['insufficient evidence','证据不足','أدلة غير كافية'],
  question_draft: ['What evidence supports this judgment about the quoted passage?','哪些证据支持评审对这段原文的判断？','ما الأدلة التي تدعم هذا الحكم على المقطع المقتبس؟'],
  decision_before_question: ['Answer the open decision before sending this question.','请先答复待处理的决定，再发送这个问题。','أجب عن القرار المعلّق قبل إرسال هذا السؤال.'],
  send_message: ['Send','发送','إرسال'],
  scope_before_reading: ['Reviewer’s judgment before reading','读稿前的判断','تقدير المراجع قبل قراءة الورقة'],
  scope_comparison: ['Comparison the design can support','研究设计能支持的比较','المقارنة التي يتيحها التصميم'],
  scope_alternative: ['Competing explanation','可能的其他解释','التفسير البديل'],
  scope_discriminator: ['Observation that would distinguish the explanations','区分这些解释所需的观察','الملاحظة التي تميّز بين التفسيرات'],
  ask_active: ['Ask a question or steer the project…','向项目提问或给出指示…','اطرح سؤالًا أو وجّه المشروع…'],
  ask_finished: ['Ask a question or request a change…','向项目提问或提出修改请求…','اطرح سؤالًا أو اطلب تعديلًا…'],
  paper_delivered: ['Your paper is ready','论文已交付','ورقتك جاهزة'],
  read_paper: ['Read the paper','阅读定稿 PDF','اقرأ الورقة'],
  review_evidence: ['Inspect the review','查看评审依据','اعرض أدلة المراجعة'],
  delivery_verified: ['The PDF matches the version signed by the independent reviewer.','这份 PDF 与独立评审签字的版本一致。','يطابق ملف PDF النسخة التي اعتمدها المراجع المستقل.'],
  conclusion_measured: ['Reviewer’s judgment: directly supported by measurements','评审判断：有直接实测支持','حكم المراجع: تدعم القياسات الاستنتاج مباشرة'],
  conclusion_interpretation: ['Reviewer’s judgment: a possible explanation','评审判断：属于可能的解释','حكم المراجع: يقدّم الاستنتاج تفسيرًا محتملًا'],
  conclusion_needs_evidence: ['Reviewer’s judgment: more evidence needed','评审判断：仍需证据','حكم المراجع: يحتاج الاستنتاج إلى أدلة إضافية'],
  delivered_pdf: ['Delivered PDF','已交付 PDF','ملف PDF المُسلَّم'], working_pdf: ['Working draft PDF','工作稿 PDF','ملف PDF للمسودة الحالية'],
  delivered_review: ['Review of the delivered paper','已交付稿评审','مراجعة الورقة المُسلَّمة'],
  working_review: ['Review of the working draft','工作稿评审','مراجعة المسودة الحالية'],
  build_error: ['The manuscript did not compile','稿件编译失败','تعذّر تجميع المخطوطة'],
  build_location: ['Typesetting error in %s, line %s','排版错误，位于 %s 第 %s 行','خطأ تنضيد في %s، السطر %s'],
  build_details: ['Compiler details','编译详情','تفاصيل التجميع'],
  paper_ready: ['Paper PDF available','论文 PDF 已生成','ملف الورقة متاح'],
  file_ready: ['Published %s','已生成证据文件 %s','ملف الأدلة متاح %s'],
  view_evidence: ['View evidence files','查看证据文件','عرض ملفات الأدلة'],
  earlier: ['Earlier events','更早的记录','السجل السابق'],
  current: ['Current action','当前工作','العمل الحالي'], observation: ['Latest observation','最新观察','أحدث ملاحظة'],
  all_phases: ['Show all phases','查看全部阶段','عرض جميع المراحل'],
  recent_observations: ['Recent observations','最近的研究进展','آخر الملاحظات'],
  latest_in_history: ['View latest in history','查看最新记录','عرض أحدث ملاحظة في السجل'],
  open_observation: ['Open in research history','在研究记录中查看','فتح في سجل البحث'],
  history: ['Research history','研究记录','سجل البحث'], evidence: ['Evidence','研究证据','الأدلة'],
  current_file: ['Open current file · %s','打开当前文件 · %s','فتح الملف الحالي · %s'],
  archived_file: ['Open archived file · %s','打开归档文件 · %s','فتح الملف المؤرشف · %s'],
  open: ['Open file','打开文件','فتح الملف'], close: ['Close','关闭','إغلاق'],
  report: ['Delivery','交付','التسليم'], review: ['Review','评审','المراجعة'],
  research: ['Research','调研','البحث'], experiment: ['Experiment','实验','التجربة'], writing: ['Writing','写作','الكتابة'],
  empty: ['No observation recorded yet.','尚无观察记录。','لم تُسجّل أي ملاحظة بعد.'],
  no_file: ['No evidence files available yet.','尚无可查看的证据文件。','لا توجد ملفات أدلة متاحة بعد.'],
  idle: ['Waiting for the next recorded action','等待下一项工作记录','بانتظار تسجيل الإجراء التالي'],
  waiting: ['Waiting for your decision','等待你的决定','بانتظار قرارك'],
  disconnected: ['Disconnected · reconnecting','连接中断，正在重连','انقطع الاتصال. جارٍ إعادة الاتصال'],
  check_quota: ['Check your provider balance, then select Continue','检查服务商余额或配额，然后点「继续」','تحقق من رصيد مزوّد الخدمة ثم اختر متابعة'],
  quota: ['Paused for API quota','因 API 配额暂停','متوقف لبلوغ حصة الواجهة'],
  paused: ['Paused','已暂停','متوقف مؤقتًا'], conversation: ['Conversation','对话','المحادثة'],
  command: ['Latest command','最近指令','آخر أمر'], pending: ['Queued','已入队','في قائمة الانتظار'],
  consumed: ['Acknowledged by worker','执行端已接收','استلم العامل الأمر'], failed: ['Failed','失败','فشل'],
  recorded: ['Answer recorded','答复已记录','تم تسجيل الرد'], decision: ['Decision','决定','القرار'],
  receipt: ['Your instruction was received.','已收到你的指令。','تم استلام تعليماتك.'],
  retry: ['Could not send. Your text is ready to retry.','发送失败，可保留原文重试。','تعذّر الإرسال. يمكنك إعادة المحاولة.'],
  reply: ['Waiting for a reply','等待回复','بانتظار الرد'], review_title: ['Independent review','独立评审','مراجعة مستقلة'],
  stale: ['Current paper awaiting review','当前稿件待评审','الورقة الحالية بانتظار المراجعة'],
  steer: ['Discuss this finding','讨论该问题','ناقش هذه الملاحظة'], diagnostics: ['Diagnostic log','诊断日志','سجل التشخيص'],
  before: ['Previous revision','上一版','النسخة السابقة'], after: ['Reviewed revision','受评版本','النسخة المراجعة'],
  unavailable: ['Preview unavailable','无法预览','المعاينة غير متاحة'], truncated: ['Preview limited to 64 KiB.','预览仅显示前 64 KiB。','تقتصر المعاينة على 64 كيلوبايت.'],
  pause_at: ['Response deadline %s','答复截止时间 %s','الموعد النهائي للرد %s'],
  trial: ['Trial','试验','التجربة'], baseline: ['Baseline','基线','خط الأساس'], proposed: ['Proposed','所提方法','الطريقة المقترحة'],
  coverage: ['What the reviewer checked','评审检查了什么','ما الذي فحصه المراجع'],
  reviewed_sources: ['Evidence inspected (%s)','查看评审依据（%s）','الأدلة المفحوصة (%s)'],
  method_controls: ['Experimental controls (%s)','实验控制项（%s）','ضوابط التجربة (%s)'],
  matches_control: ['Matches','一致','مطابق'], differs_control: ['Mismatch','不一致','غير مطابق'],
  event_review_started: ['Independent review opened','独立评审已开始','بدأت المراجعة المستقلة'],
  event_evidence_scope_committed: ['Reviewer recorded the pre-reading judgment','评审已记录读稿前的判断','سجّل المراجع تقديره قبل قراءة الورقة'],
  event_measurement_reproduction_started: ['Recomputing a recorded result','正在独立复算','إعادة حساب نتيجة مسجلة'],
  event_measurement_reproduction_finished: ['Independent calculation complete','独立复算已完成','اكتملت إعادة الحساب'],
  event_measurement_reproduction_failed: ['Calculation could not finish','复算未完成','تعذر إكمال الحساب'],
  event_measurement_reproduction_unavailable: ['Reproduction unavailable','当前无法复算','إعادة الحساب غير متاحة'],
  event_method_control_started: ['Testing a prediction','正在检验预测','اختبار التوقع'],
  event_method_control_finished: ['Prediction checked','预测检验已完成','اكتمل اختبار التوقع'],
  event_method_control_failed: ['Prediction test unavailable','预测检验未完成','تعذر اختبار التوقع'],
  after_review_accept: ['Review complete. Awaiting the next step.','本版评审已完成，等待下一步。','اكتملت مراجعة هذه النسخة. بانتظار الخطوة التالية.'],
  after_review_revise: ['The reviewer requested a revision.','评审要求修改本版稿件。','طلب المراجع تعديل هذه النسخة.'],
  after_review_unassessed: ['The review ended without a verdict.','本版评审未能给出结论。','انتهت مراجعة هذه النسخة دون حسم.'],
  event_manuscript_reviewed: ['Verdict recorded','评审结论已归档','حُفظ قرار المراجعة'],
  row_start: ['Independent calculation started for %s.','正在复算 %s。','بدأ الحساب المستقل للحالة %s.'],
  row_attempt: ['Independent calculation for %s · attempt %s','正在复算 %s · 第 %s 次尝试','إعادة حساب %s · المحاولة %s'],
  row_format: ['Attempt %s returned data in the wrong shape. No comparison is available.','第 %s 次复算返回的数据格式有误，尚无可比较的数值。','أعادت المحاولة %s بيانات بتنسيق غير صالح. لم تتوفر مقارنة.'],
  row_execution: ['Attempt %s ended before it produced a measurement.','第 %s 次复算在得到测量值前中止。','انتهت المحاولة %s قبل الحصول على قياس.'],
  row_match: ['%s · All %s outcomes matched.','%s · %s 项指标全部吻合。','%s · تطابقت النتائج كلها (%s).'],
  row_diff: ['%s · %s of %s outcomes differ.','%s · %s 项存在差异，共 %s 项。','%s · تختلف %s من أصل %s نتائج.'],
  row_values: ['%s was reported as %s and recomputed as %s.','%s 的论文报告值为 %s，独立复算为 %s。','%s: القيمة المسجلة %s وإعادة الحساب %s.'],
  control_metric: ['Outcome','指标','المقياس'], control_recomputed: ['Recomputed','独立复算','أُعيد حسابه'],
  compared_metric: ['Recomputed metric','复算指标','المقياس المُعاد حسابه'],
  compare_values: ['Compare values','查看数值对比','مقارنة القيم'],
  control_reported: ['Reported','论文报告值','المسجل'], control_result: ['Comparison','比较','المقارنة'],
  control_expected: ['Expected','预期值','المتوقع'], control_observed: ['Observed','观察值','المرصود'],
  control_rationale: ['Reviewer’s rationale','评审理由','تعليل المراجع'], control_source: ['Source','证据来源','المصدر'],
  measured: ['Plotted from measurements','由实测数据绘制','رُسم من القياسات'],
  copied: ['Values copied into plotting code','绘图代码写死了数值','القيم منسوخة في كود الرسم'],
  theoretical: ['Theoretical curve','理论曲线','منحنى نظري'],
  manuscript: ['Manuscript','论文正文','المخطوطة'], citations: ['Citations','引用','الاستشهادات'],
  visual: ['Rendered pages and figures','排版与图表','الصفحات والأشكال'], assessed: ['Assessed','已核查','تم تقييمه'],
  blocker: ['Blocking issue','阻断性问题','مشكلة مانعة'], minor: ['Minor issue','次要问题','ملاحظة طفيفة'], question: ['Question','待澄清','سؤال'],
  no_score: ['Awaiting review','等待评审','بانتظار المراجعة'], accept: ['Accepted','已通过','مقبول'], revise: ['Needs revision','需要修改','يتطلب التعديل'],
  unassessed: ['Unassessed','尚未评估','لم يقيّم'], new: ['Open','待修复','مفتوح'], still_present: ['Still present','问题仍存在','ما زالت قائمة'], resolved: ['Resolved','已解决','تم الحل'],
  seconds: ['%s s','%s 秒','%s ث'], minutes: ['%s min','%s 分钟','%s د'],
  page: ['Page','页','صفحة'], elapsed: ['%s since observation','距上次记录 %s','%s منذ آخر ملاحظة']
};
Object.entries(researchLabels).forEach(([key, values]) =>
  ['en','zh','ar'].forEach((lang, i) => TRANSLATIONS[lang]['research_' + key] = values[i]));
const rt = (key, ...args) => t('research_' + key, ...args);
const re = id => document.getElementById(id);
let researchProject = {}, researchData = {}, researchEvents = new Map(), researchCursor = 0;
let historyPhase = '';
let showWorkingReview = false;
let fileSignature = '', reviewSignature = '', preview = null, streamReady = false, disconnected = false;

function resetResearch(project) {
  closeEvidence(false);
  if (researchProject.id !== project.id && re('hitl-compose').dataset.review) {
    re('hitl-input').value = '';
    clearReviewDraft();
  }
  researchProject = project;
  researchEvents.clear();
  historyPhase = '';
  showWorkingReview = false;
  researchData = {};
  researchCursor = 0;
  fileSignature = reviewSignature = '';
  disconnected = streamReady = false;
  re('research-history').open = false;
  re('research-history').querySelector('ol').replaceChildren();
  re('research-announce').textContent = '';
  re('command-receipt').textContent = '';
  receiveResearch(project.research || {}, false);
}

function receiveResearch(record, animate) {
  let changed = false;
  for (const event of record.events || []) {
    if (!event.id || researchEvents.has(event.id)) continue;
    researchEvents.set(event.id, event);
    changed = true;
  }
  researchCursor = record.cursor ?? researchCursor;
  const primary = record.evidence?.find(item => item.key === 'paper/main.pdf');
  const oldPrimary = researchData.evidence?.find(item => item.key === 'paper/main.pdf');
  const pdfChanged = JSON.stringify(primary) !== JSON.stringify(oldPrimary) || record.has_working_pdf !== researchData.has_working_pdf;
  researchData = record;
  researchProject.research = record;
  researchProject.has_pdf = !!primary;
  if (pdfChanged) renderDetailActions(researchProject);
  renderResearch(animate && changed);
  if (changed || !researchEvents.size) renderHistory();
  renderEvidence(animate && changed);
  renderReview();
  const cmd = record.command;
  const receipts = [];
  if (cmd) receipts.push([rt('command'),cmd.kind,researchLabels[cmd.status] ? rt(cmd.status) : cmd.status].join(' · '));
  if (record.decision_receipt) receipts.push(rt('decision') + ' · ' + rt(record.decision_receipt));
  const text = receipts.join(' / ');
  if (re('command-receipt').textContent !== text) re('command-receipt').textContent = text;
}

function researchPhase(phase) {
  return ({ experiments:'experiment', figures:'writing', dev:'experiment', development:'experiment', planning:'research', plan:'research', literature:'research', analysis:'experiment',
    draft:'writing', manuscript:'writing', reviewer:'review', delivered:'report', delivery:'report', accepted:'report' })[phase] || phase;
}

function reviewEventLabel(event) {
  const label = 'event_' + event?.action;
  return researchLabels[label] ? rt(label) : '';
}

function reviewHandoff(recent) {
  const review = researchData.working_review || researchData.review;
  if (!review || !['accept','revise','unassessed'].includes(review.verdict) || !/^[a-f0-9]{64}$/.test(review.revision)) return '';
  const archive = 'auto_research/state/reviews/' + review.revision + '/main.pdf';
  const signed = recent.findIndex(e => e.action === 'manuscript_reviewed' && e.revision === review.revision &&
    e.observation === review.verdict && e.artifact === archive);
  if (signed < 0) return '';
  // The enclosing owner tool returns after recording the verdict. A new tool
  // invocation or scientific observation records the next piece of work.
  const next = recent.slice(0,signed).find(e => e.observation && !['tool/result','turn/end','turn_end'].includes(e.action));
  if (next) return reviewEventLabel(next) || observationText(next);
  return review.stale ? rt('stale') : rt('after_review_' + review.verdict);
}

function observationText(event, withValues=true) {
  if (event?.action === 'evidence_scope_committed') return reviewEventLabel(event);
  if (event?.action === 'delivered' && event.artifact === 'paper/main.pdf' && /^[a-f0-9]{64}$/.test(event.revision))
    return rt('delivery_verified');
  if (event?.action === 'manuscript_reviewed' && ['accept','revise','unassessed'].includes(event.observation))
    return rt(event.observation);
  if (event?.action === 'measurement_reproduction_started' && event.condition)
    return event.attempt ? rt('row_attempt', event.condition, event.attempt) : rt('row_start', event.condition);
  if (event?.action === 'measurement_reproduction_failed' && event.attempt && ['row_format','execution'].includes(event.failure))
    return rt(event.failure === 'row_format' ? 'row_format' : 'row_execution', event.attempt);
  if (event?.condition && event.action === 'measurement_reproduction_finished' && Number.isInteger(event.outcomes)) {
    if (event.differed === 0) return rt('row_match', event.condition, event.outcomes);
    if (event.differed > 0 && event.differed <= event.outcomes) {
      const summary = rt('row_diff', event.condition, event.differed, event.outcomes);
      return withValues && hasComparison(event)
        ? summary + ' ' + rt('row_values', event.metric, event.reported_value, event.independent_value) : summary;
    }
  }
  return event?.observation || '';
}

function hasComparison(event) {
  return event?.action === 'measurement_reproduction_finished' && event.condition &&
    Number.isInteger(event.outcomes) && event.differed > 0 && event.differed <= event.outcomes &&
    event.metric && event.reported_value && event.independent_value;
}

function fillComparison(values, event) {
  const [reported, independent] = values.querySelectorAll('dd');
  if (reported.textContent !== event.reported_value) reported.textContent = event.reported_value;
  if (independent.textContent !== event.independent_value) independent.textContent = event.independent_value;
}

function renderObservationTrail(observations, animate) {
  const trail = re('research-trail');
  const recent = observations.slice(0,3).reverse();
  const signature = JSON.stringify([researchProject.id,currentLang,...recent.map(e => e.id)]);
  if (trail.dataset.events === signature) return;
  trail.dataset.events = signature;
  trail.hidden = recent.length < 2;
  trail.setAttribute('aria-label',rt('recent_observations'));
  if (trail.hidden) { trail.replaceChildren(); return; }
  const focused = document.activeElement.closest('#research-trail button');
  const visible = new Set(recent.map(e => e.id));
  for (const row of [...trail.children]) if (!visible.has(row.dataset.event)) row.remove();
  for (const [index,event] of recent.entries()) {
    let item = [...trail.children].find(row => row.dataset.event === event.id);
    if (!item) {
      item = document.createElement('li');
      item.dataset.event = event.id;
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('aria-controls','research-history');
      button.append(document.createElement('small'),document.createElement('span'));
      button.onclick = () => {
        selectHistoryPhase('');
        const entry = [...re('research-history').querySelector('ol').children].find(row => row.dataset.event === event.id);
        entry?.focus();
      };
      item.append(button);
      trail.append(item);
      if (animate && event === recent.at(-1) && !disconnected && matchMedia('(prefers-reduced-motion: no-preference)').matches)
        item.animate([{opacity:0,transform:'translateY(5px)'},{opacity:1,transform:'none'}],
                     {duration:240,easing:'cubic-bezier(.2,.7,.3,1)'});
    }
    if (trail.children[index] !== item) trail.insertBefore(item,trail.children[index] || null);
    const button = item.firstElementChild, phase = researchPhase(event.phase);
    const when = new Date(event.ts);
    const time = event.ts && !Number.isNaN(when.getTime()) ? when.toLocaleTimeString(currentLang) : '';
    const label = researchLabels[phase] ? rt(phase) : '';
    button.firstElementChild.textContent = [label,time].filter(Boolean).join(' · ');
    const observation = observationText(event,false);
    button.lastElementChild.textContent = event === recent.at(-1) ? rt('latest_in_history') : observation;
    button.title = rt('open_observation') + ' · ' + observation;
    button.setAttribute('aria-label', button.title);
  }
  if (focused && !trail.contains(document.activeElement))
    (focused.isConnected ? focused : trail.querySelector('button')).focus({preventScroll:true});
}

function renderHistory() {
  const history = re('research-history');
  history.hidden = !researchEvents.size;
  re('history-earlier').hidden = !([...researchEvents.values()][0]?.offset > 0);
  const all = re('history-all');
  all.hidden = !historyPhase;
  history.querySelector('summary').textContent = historyPhase ? rt('history') + ' · ' + rt(historyPhase) : rt('history');
  const visible = [...researchEvents.values()].reverse().filter(e => !historyPhase || researchPhase(e.phase) === historyPhase);
  const list = history.querySelector('ol');
  list.classList.toggle('history-filtered', !!historyPhase);
  const existing = new Map([...list.children].map(row => [row.dataset.event, row]));
  const visibleIds = new Set(visible.map(e => e.id));
  for (const [id,row] of existing) if (!visibleIds.has(id)) row.remove();
  for (const [index,e] of visible.entries()) {
    let row = existing.get(e.id);
    if (!row || row.lang !== currentLang) {
      row = document.createElement('li');
      row.dataset.event = e.id;
      row.tabIndex = -1;
      row.lang = currentLang;
      const meta = document.createElement('div'), time = document.createElement('time');
      meta.className = 'history-meta';
      const phase = researchPhase(e.phase);
      const label = document.createElement('span'), action = reviewEventLabel(e);
      label.textContent = action || (researchLabels[phase] ? rt(phase) : e.action);
      if (!action && researchLabels[phase]) label.className = 'history-phase';
      label.title = e.action;
      meta.append(label);
      if (e.ts) {
        time.dateTime = e.ts;
        time.textContent = fmtDate(e.ts);
        meta.append(time);
      }
      const observation = document.createElement('p');
      const progress = e.count != null && e.total > 0 ? ' (' + e.count + '/' + e.total + ')' : '';
      observation.textContent = (observationText(e, false) || e.action) + progress;
      row.append(meta, observation);
      if (hasComparison(e)) {
        const details = document.createElement('details'), summary = document.createElement('summary');
        details.className = 'history-comparison';
        const metric = document.createElement('bdi');
        metric.textContent = e.metric;
        summary.append(rt('compare_values') + ' · ', metric);
        const values = re('research-comparison').querySelector('dl').cloneNode(true);
        fillComparison(values, e);
        details.append(summary, values);
        row.append(details);
      }
      if (e.artifact && !window.SHARE_MODE) {
        const delivered = e.action === 'delivered' && e.artifact === 'paper/main.pdf' && /^[a-f0-9]{64}$/.test(e.revision);
        const key = delivered ? 'auto_research/state/reviews/' + e.revision + '/main.pdf' : e.artifact;
        const name = key.split('/').pop();
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'history-file';
        const archived = /^auto_research\/state\/reviews\/[a-f0-9]{64}\//.test(key);
        button.textContent = rt(archived ? 'archived_file' : 'current_file', name);
        button.title = key;
        button.setAttribute('aria-controls','evidence-preview');
        button.setAttribute('aria-pressed','false');
        button.onclick = () => openEvidence({key, label:name, edition:key === 'paper/main.pdf' ? 'working' : null}, button);
        row.append(button);
      }
      existing.get(e.id)?.replaceWith(row);
    }
    // Keeping existing rows attached preserves keyboard focus and the preview's
    // return target when a live event arrives above the reader's position.
    if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
  }
}

function selectHistoryPhase(phase) {
  historyPhase = phase;
  re('research-history').open = true;
  for (const button of re('research-phases').querySelectorAll('button'))
    button.setAttribute('aria-pressed', String(button.dataset.phase === phase));
  renderHistory();
}

re('history-all').onclick = () => {
  selectHistoryPhase('');
  re('research-history').querySelector('summary').focus();
};

re('history-earlier').onclick = async function () {
  const id = detailProjectId, first = [...researchEvents.values()][0]?.offset;
  if (!first) return;
  this.disabled = true;
  try {
    const response = await fetch(APP_BASE + '/api/projects/' + encodeURIComponent(id) + '/research-history?before=' + first);
    if (!response.ok) throw Error();
    const {events} = await response.json();
    if (id !== detailProjectId) return;
    const earlier = events.map(e => [e.id,e]);
    researchEvents = new Map([...earlier,...researchEvents]);
    renderResearch();
    renderHistory();
    if (!earlier.length) this.hidden = true;
  } catch { if (id === detailProjectId) re('research-announce').textContent = rt('unavailable'); }
  finally { this.disabled = false; }
};

function renderResearch(animate=false) {
  const p = researchProject, events = [...researchEvents.values()], last = events.at(-1);
  const recent = events.slice().reverse();
  const observations = recent.filter(e => e.observation && !['tool/call','tool/result','turn/end','turn_end'].includes(e.action));
  const obs = observations[0] || recent.find(e => e.observation && e.action !== 'tool/call');
  renderObservationTrail(observations, animate);
  const rawPhase = recent.find(e => e.phase && e.phase !== 'work')?.phase || p.phase_status?.phase;
  const phase = researchPhase(rawPhase);
  const rail = re('research-phases');
  const visited = new Set(events.map(e => researchPhase(e.phase)).filter(Boolean));
  const visits = [...visited].sort().join(' ');
  if (rail.dataset.phase !== (phase || '') || rail.dataset.visits !== visits || rail.lang !== currentLang) {
    const focused = rail.contains(document.activeElement) && document.activeElement.dataset.phase;
    rail.dataset.visits = visits;
    const previous = rail.dataset.phase;
    rail.dataset.phase = phase || '';
    rail.lang = currentLang;
    rail.replaceChildren(...['research','experiment','writing','review','report'].map(key => {
      const li = document.createElement('li'), label = document.createElement(visited.has(key) ? 'button' : 'span');
      label.className = 'research-phase';
      label.textContent = rt(key);
      if (visited.has(key)) {
        label.type = 'button';
        label.dataset.phase = key;
        label.title = rt('history') + ' · ' + rt(key);
        label.setAttribute('aria-controls', 'research-history');
        label.setAttribute('aria-pressed', String(historyPhase === key));
        label.onclick = () => selectHistoryPhase(historyPhase === key ? '' : key);
      }
      if (key === phase) {
        li.setAttribute('aria-current','step');
        if (animate && previous !== phase && !disconnected && !p.pending_decision && p.status === 'running' && p.control_state !== 'paused') li.classList.add('research-arrived');
      }
      li.append(label);
      return li;
    }));
    if (focused) rail.querySelector('button[data-phase="' + focused + '"]')?.focus({preventScroll:true});
  }
  re('research-stop').hidden = !!window.SHARE_MODE || !['queued','running'].includes(p.status);
  const active = ['initializing','queued','pending','running'].includes(p.status);
  const waiting = p.pending_decision && p.pending_decision.id !== _lastAnsweredDecision;
  const quota = /quota[_ -]?(paused|exhausted)|insufficient[_ ]balance/i.test(
    [p.control_state, last?.status, last?.action === 'quota' ? 'quota_paused' : '', p.error_message].filter(Boolean).join(' '));
  const paused = p.control_state === 'paused';
  const label = t('status_' + p.status);
  const state = (quota ? rt('quota') : active && waiting ? rt('waiting') : paused ? rt('paused') :
    label.startsWith('status_') ? (p.status || '') : label).replace(/^[^\p{L}\p{N}]+/u, '');
  const delivered = p.status === 'done' && researchData.evidence?.find(item => item.edition === 'delivered');
  re('research-state').textContent = disconnected ? rt('disconnected') : state;
  re('research-state').hidden = Boolean(delivered && !disconnected);
  const paper = re('research-read-paper');
  paper.hidden = !delivered;
  if (delivered) {
    const base = APP_BASE + '/api/projects/' + encodeURIComponent(detailProjectId);
    paper.href = base + '/pdf?delivered=' + encodeURIComponent(delivered.delivered_sha256);
    const cover = paper.querySelector('img');
    const revision = researchData.review?.revision;
    const showCover = !window.SHARE_MODE && /^[a-f0-9]{64}$/.test(revision);
    if (showCover) {
      const key = 'auto_research/state/reviews/' + revision + '/main.pdf';
      const src = base + '/evidence?key=' + encodeURIComponent(key) + '&sha256=' + delivered.delivered_sha256 + '&page=1';
      if (cover.getAttribute('src') !== src) {
        cover.hidden = true;
        cover.onload = () => { cover.hidden = false; };
        cover.onerror = () => { cover.hidden = true; };
        cover.src = src;
      }
    } else {
      cover.hidden = true;
      cover.removeAttribute('src');
    }
  } else {
    paper.removeAttribute('href');
    paper.querySelector('img').removeAttribute('src');
  }
  const action = quota ? rt('check_quota') : active && waiting ? p.pending_decision.question : !active || paused ? state :
    (p.status === 'running' && reviewHandoff(recent)) || p.activity || reviewEventLabel(last) || (last?.status === 'running' && (last.action === 'tool/call' ? last.observation : last.action)) || (p.status !== 'running' ? state : rt('idle'));
  re('research-action').textContent = delivered ? rt('paper_delivered') : action;
  const artifact = researchData.evidence?.[0];
  const fallback = artifact?.key === 'paper/main.pdf' ? rt('paper_ready') :
    artifact ? rt('file_ready', artifact.label) : rt('empty');
  const diagnostic = obs?.action === 'paper_compile_failed' ? obs.observation : '';
  const location = diagnostic?.match(/(?:^|\s)(?:\.\/)?([\w./-]+\.(?:tex|sty|bbl|cls)):(\d+):/m);
  const details = re('research-build-details');
  if (details.dataset.event !== (diagnostic ? obs.id : '')) {
    details.dataset.event = diagnostic ? obs.id : '';
    details.open = false;
    details.querySelector('pre').textContent = diagnostic || '';
  }
  details.hidden = !diagnostic;
  const observed = diagnostic ? (location ? rt('build_location',location[1],location[2]) : rt('build_error')) :
    observationText(obs) || (delivered ? rt('delivery_verified') : researchData.milestone?.text || fallback);
  const comparison = re('research-comparison');
  const summary = observationText(obs, false);
  const showValues = hasComparison(obs);
  comparison.hidden = !showValues;
  if (showValues) {
    comparison.querySelector('bdi').textContent = obs.metric;
    fillComparison(comparison, obs);
  }
  const measured = Number.isInteger(obs?.count) && Number.isInteger(obs.total) &&
    obs.count >= 0 && obs.total > 0 && obs.count <= obs.total;
  const counter = measured ? ' (' + obs.count + '/' + obs.total + ')' : '';
  const meter = re('research-count');
  meter.hidden = !measured;
  meter.classList.toggle('research-count-live', animate && active && !disconnected && !waiting && !paused && !quota);
  if (measured) meter.firstElementChild.style.transform = 'scaleX(' + obs.count / obs.total + ')';
  const observation = re('research-observation');
  if (observation.dataset.description !== observed + counter) {
    observation.dataset.description = observed + counter;
    observation.textContent = (showValues ? summary : observed) + counter;
    if (animate && active && !disconnected && !paused && !waiting && !quota &&
        window.matchMedia('(prefers-reduced-motion: no-preference)').matches) {
      for (const el of showValues ? [observation, comparison] : [observation]) {
        el.getAnimations().forEach(animation => animation.cancel());
        el.animate([{opacity:.6, transform:'translateY(3px)'}, {opacity:1, transform:'none'}],
          {duration:220, easing:'ease-out'});
      }
    }
  }
  const ts = obs?.ts || researchData.milestone?.ts;
  re('research-time').textContent = ts ? fmtDate(ts) : '';
  if (ts && active && !waiting && !paused && !disconnected) {
    const seconds = Math.floor((Date.now() - Date.parse(ts)) / 1000);
    if (seconds >= 0) re('research-time').textContent += ' · ' + rt('elapsed', seconds < 60 ? rt('seconds', seconds) : rt('minutes', Math.floor(seconds / 60)));
  }
  if (animate && !disconnected && !waiting && !paused && re('research-announce').textContent !== observed + counter)
    re('research-announce').textContent = observed + counter;
}

function filesForProject() {
  const files = new Map();
  for (const item of researchData.evidence || []) {
    files.set(item.key, item.edition ? {...item, label:rt(item.edition + '_pdf')} : item);
  }
  for (const e of [...researchEvents.values()].reverse()) {
    if (e.artifact && !files.has(e.artifact)) files.set(e.artifact, { key:e.artifact, label:e.revision ? e.artifact.split('/').pop() + ' · ' + e.revision.slice(0,10) : e.artifact.split('/').pop() });
  }
  return [...files.values()];
}
function renderEvidence(animate) {
  const files = filesForProject(), signature = JSON.stringify([currentLang, files]);
  const opener = re('research-open-evidence');
  const delivered = researchProject.status === 'done' && files.some(item => item.edition === 'delivered');
  const reviewAvailable = delivered && !window.SHARE_MODE && researchData.review?.checks?.length;
  opener.hidden = !files.length || (delivered && !reviewAvailable);
  opener.textContent = rt(reviewAvailable ? 'review_evidence' : 'view_evidence');
  opener.onclick = () => {
    if (reviewAvailable) {
      showWorkingReview = false;
      renderReview();
      const coverage = re('review-coverage');
      coverage.open = true;
      coverage.querySelector('summary').focus();
      return;
    }
    const obs = [...researchEvents.values()].reverse().find(e => e.observation && !['tool/call','tool/result','turn/end','turn_end'].includes(e.action));
    const file = files.find(f => f.key === obs?.artifact);
    if (file) openEvidence(file,re('research-open-evidence'));
    else re('evidence-heading').focus();
  };
  if (fileSignature === signature) return;
  fileSignature = signature;
  const target = re('evidence-files');
  if (!files.length) {
    const p = document.createElement('p'); p.textContent = rt('no_file'); target.replaceChildren(p);
    return;
  }
  const existing = new Map([...target.querySelectorAll('button')].map(b => [b.dataset.key,b]));
  target.querySelector('p')?.remove();
  for (const item of files) {
    let btn = existing.get(item.key);
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button'; btn.dataset.key = item.key;
      const type = document.createElement('span'), name = document.createElement('span');
      type.className = 'file-type'; name.className = 'file-name';
      type.textContent = item.key.split('.').pop().toUpperCase();
      btn.title = item.key; btn.append(type,name);
      if (animate) btn.classList.add('research-arrived');
      target.append(btn);
    }
    existing.delete(item.key);
    btn.querySelector('.file-name').textContent = item.label || item.key;
    btn.onclick = () => openEvidence(item,btn);
    btn.setAttribute('aria-pressed', String(preview?.key === item.key));
  }
  for (const btn of existing.values()) btn.remove();
}

function closeEvidence(focus=true) {
  const old = preview;
  preview?.controller.abort();
  preview = null;
  re('evidence-preview').hidden = true;
  re('preview-content').replaceChildren();
  old?.opener?.setAttribute('aria-pressed','false');
  if (focus && old?.opener?.isConnected) old.opener.focus();
}
re('evidence-close').onclick = () => closeEvidence();
re('evidence-preview').onkeydown = event => { if (event.key === 'Escape') closeEvidence(); };

async function openEvidence(item, opener, page=1, compare=false) {
  const container = re('preview-content'), scroll = compare ? container.scrollTop : 0;
  closeEvidence(false);
  const controller = new AbortController();
  preview = {key:item.key, opener, controller};
  opener.setAttribute('aria-pressed','true');
  const base = APP_BASE + '/api/projects/' + encodeURIComponent(detailProjectId);
  const media = item.type || ({pdf:'application/pdf',png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg'})[item.key.split('.').pop()] || 'text/plain';
  const pdfQuery = item.delivered_sha256 ? '?delivered=' + encodeURIComponent(item.delivered_sha256) :
    item.edition === 'working' && !window.SHARE_MODE ? '?working=true' : '';
  const url = item.key === 'paper/main.pdf' && !item.sha256 ? base + '/pdf' + pdfQuery : base + '/evidence?key=' + encodeURIComponent(item.key) + (item.sha256 ? '&sha256=' + encodeURIComponent(item.sha256) : '');
  const href = media === 'application/pdf' ? url + '#page=' + Math.max(1,Number(page)||1) : url;
  re('evidence-preview').hidden = false;
  re('preview-name').textContent = item.label || item.key.split('/').pop();
  re('evidence-open').href = href;
  if (!compare) re('preview-name').focus({preventScroll:false});
  if (media.startsWith('image/') || media === 'application/pdf') {
    const rasterPage = media === 'application/pdf' && item.sha256 && page > 0;
    const view = document.createElement(media === 'application/pdf' && !rasterPage ? 'iframe' : 'img');
    view.title = view.alt = re('preview-name').textContent;
    view.onload = () => { container.scrollTop = scroll; };
    view.onerror = () => { if (!controller.signal.aborted) container.textContent = rt('unavailable'); };
    view.src = rasterPage ? url + '&page=' + page : href; container.replaceChildren(view);
    return;
  }
  try {
    const r = await fetch(url, {signal:controller.signal});
    if (!r.ok) throw Error();
    const reader = r.body.getReader(), decoder = new TextDecoder();
    let text = '', remaining = 65536;
    while (remaining > 0) {
      const {value, done} = await reader.read();
      if (done) break;
      text += decoder.decode(value.subarray(0,remaining), {stream:true});
      remaining -= value.length;
    }
    text += decoder.decode();
    if (remaining <= 0) { await reader.cancel(); text += '\n' + rt('truncated'); }
    if (controller.signal.aborted) return;
    let rows;
    try { rows = JSON.parse(text).rows; } catch {}
    if (Array.isArray(rows) && rows.length && rows.every(row => row && typeof row.label === 'string' && Number.isFinite(row.baseline) && Number.isFinite(row.proposed))) {
      const table = document.createElement('table'), head = table.createTHead().insertRow();
      for (const key of ['trial','baseline','proposed']) { const th = document.createElement('th'); th.textContent = rt(key); head.append(th); }
      const body = table.createTBody();
      for (const row of rows) { const tr = body.insertRow(); for (const k of ['label','baseline','proposed']) tr.insertCell().textContent = row[k]; }
      container.replaceChildren(table);
    } else {
      const pre = document.createElement('pre'), start = item.quote ? text.indexOf(item.quote) : -1;
      if (start >= 0) {
        const mark = document.createElement('mark');
        mark.textContent = item.quote;
        pre.append(document.createTextNode(text.slice(0,start)), mark, document.createTextNode(text.slice(start + item.quote.length)));
        container.replaceChildren(pre);
        container.scrollTop += mark.getBoundingClientRect().top - container.getBoundingClientRect().top - 24;
      } else {
        pre.textContent = text; container.replaceChildren(pre);
      }
    }
  } catch { if (!controller.signal.aborted) container.textContent = rt('unavailable'); }
}

function reviewReference(ref) {
  const name = document.createElement(ref.key ? 'button' : 'strong');
  name.textContent = [ref.artifact, ref.page && rt('page') + ' ' + ref.page].filter(Boolean).join(' · ');
  if (ref.key) {
    name.type = 'button';
    name.onclick = () => openEvidence({...ref, label:name.textContent}, name, ref.page);
  }
  return name;
}

function reviewSources(evidence, questionFor) {
  const sources = document.createElement('details'), summary = document.createElement('summary');
  sources.className = 'review-passages';
  summary.textContent = rt('reviewed_sources', evidence.length);
  sources.append(summary);
  for (const ref of evidence) {
    const passage = document.createElement('div');
    passage.append(reviewReference(ref));
    if (ref.quote) {
      const quote = document.createElement('pre');
      quote.dir = 'auto'; quote.textContent = ref.quote;
      passage.append(quote);
    }
    const ask = questionFor?.(ref);
    if (ask) passage.append(ask);
    sources.append(passage);
  }
  return sources;
}

function renderReview() {
  const delivered = researchData.evidence?.some(item => item.edition === 'delivered');
  const other = researchData.working_review;
  if (!other) showWorkingReview = false;
  const review = showWorkingReview ? other : researchData.review;
  const key = JSON.stringify([currentLang,review,delivered,!!other,showWorkingReview]);
  if (key === reviewSignature) return;
  reviewSignature = key;
  re('research-review').hidden = !review || window.SHARE_MODE;
  if (!review) return;
  re('review-heading').textContent = rt(delivered ? (showWorkingReview ? 'working_review' : 'delivered_review') : 'review_title');
  const toggle = re('review-switch');
  toggle.hidden = !other;
  if (other) {
    toggle.textContent = rt(showWorkingReview ? 'delivered_review' : 'working_review');
    toggle.onclick = () => { showWorkingReview = !showWorkingReview; renderReview(); };
  }
  const verdict = review.stale ? rt('stale') : (researchLabels[review.verdict] ? rt(review.verdict) : review.verdict);
  re('review-verdict').textContent = [verdict, review.revision?.slice(0,12), review.summary].filter(Boolean).join(' · ');
  const coverage = re('review-coverage');
  coverage.hidden = !review.checks?.length;
  coverage.querySelector('ul').replaceChildren(...(review.checks || []).map(check => {
    const li = document.createElement('li'), label = document.createElement('strong');
    label.textContent = check.conclusion_support ? rt('conclusion_' + check.conclusion_support) :
      rt(check.domain) + ' · ' + rt(check.status);
    if (check.conclusion_support) label.className = 'review-judgment';
    li.append(label, document.createTextNode(check.reason ? ' ' + check.reason : ''));
    if (check.evidence_scope) {
      const details = document.createElement('details'), summary = document.createElement('summary'), fields = document.createElement('dl');
      details.className = 'review-scope';
      summary.textContent = rt('scope_before_reading');
      for (const key of ['comparison','alternative','discriminator']) {
        const term = document.createElement('dt'), value = document.createElement('dd');
        term.textContent = rt('scope_' + key); value.textContent = check.evidence_scope[key];
        fields.append(term, value);
      }
      details.append(summary, fields); li.append(details);
    }
    const passages = !check.claims?.length && !window.SHARE_MODE && check.conclusion_support ? (check.evidence || []).filter(ref =>
      ref.artifact === 'paper/main.pdf' && ref.page > 0 && ref.quote && !ref.previous) : [];
    function questionButton(passage, judgment=label.textContent, action='question_conclusion') {
      const ask = document.createElement('button');
      ask.type = 'button';
      ask.textContent = rt(action, passage.page);
      ask.className = 'question-conclusion';
      ask.setAttribute('aria-controls', 'hitl-compose');
      ask.onclick = () => {
        const input = re('hitl-input'), compose = re('hitl-compose');
        const source = [passage.artifact, rt('page') + ' ' + passage.page].join(' · ');
        const citation = judgment + '\n' + source + '\n“' + passage.quote + '”\n' + rt('review') + ' ' + review.revision;
        const question = input.value.trimEnd() || rt('question_draft');
        if (!input.value.includes(citation)) {
          input.value = question + '\n\n' + citation;
          compose.dataset.review = review.revision;
          input.rows = 8;
          input.dispatchEvent(new Event('input'));
          input.setSelectionRange(question.length, question.length);
        }
        updateReviewDraft();
        input.focus({preventScroll:true});
        input.scrollIntoView({block:'center', behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
      };
      return ask;
    }
    if (check.claims?.length) {
      const list = document.createElement('ul');
      list.className = 'review-claims';
      for (const claim of check.claims) {
        const row = document.createElement('li'), heading = document.createElement('strong');
        const quote = document.createElement('blockquote'), basis = document.createElement('strong'), reason = document.createElement('p');
        heading.textContent = rt('claim_' + claim.kind);
        quote.dir = 'auto'; quote.textContent = claim.passage.quote;
        basis.textContent = rt('claim_basis', rt('basis_' + claim.basis));
        reason.dir = 'auto'; reason.textContent = claim.reason;
        const actions = document.createElement('div');
        actions.className = 'review-controls';
        actions.append(reviewReference(claim.passage));
        if (!window.SHARE_MODE) actions.append(questionButton(claim.passage, [heading.textContent, basis.textContent, claim.reason].join('\n'), 'question_' + claim.kind));
        row.append(heading, quote, basis, reason, actions);
        if (claim.evidence.length) row.append(reviewSources(claim.evidence));
        list.append(row);
      }
      li.append(list);
    }
    if (passages.length === 1) {
      const controls = document.createElement('div'), ask = questionButton(passages[0]);
      controls.className = 'review-controls';
      controls.append(ask); li.append(controls);
    }
    if (check.controls?.length) {
      const details = document.createElement('details'), summary = document.createElement('summary');
      details.className = 'method-controls';
      summary.textContent = rt('method_controls', check.controls.length);
      const list = document.createElement('ul');
      for (const control of check.controls) {
        const row = document.createElement('li'), heading = document.createElement('strong');
        const target = document.createElement('bdi');
        target.textContent = control.target;
        heading.append(target, document.createTextNode(' · ' + rt(control.matches ? 'matches_control' : 'differs_control')));
        row.append(heading);
        if (control.outcomes?.length) {
          const table = document.createElement('table'), header = table.createTHead().insertRow();
          table.setAttribute('aria-label', control.target);
          for (const label of ['control_metric','control_recomputed','control_reported','control_result']) {
            const th = document.createElement('th'); th.scope = 'col'; th.textContent = rt(label); header.append(th);
          }
          const body = table.createTBody();
          for (const outcome of control.outcomes) {
            const line = body.insertRow(), metric = document.createElement('th');
            line.dataset.match = String(outcome.matches);
            metric.scope = 'row'; metric.textContent = outcome.name; line.append(metric);
            for (const text of [outcome.expected, outcome.reported, rt(outcome.matches ? 'matches_control' : 'differs_control')])
              line.insertCell().textContent = text;
          }
          row.append(table);
        }
        const fields = document.createElement('dl');
        for (const [label, value] of [
          ...(control.outcomes?.length ? [] : [['control_expected', control.expected], ['control_observed', control.observed]]),
          ['control_rationale', control.rationale], ['control_source', control.artifact + ' · SHA-256 ' + control.sha256]
        ]) {
          const term = document.createElement('dt'), description = document.createElement('dd');
          term.textContent = rt(label);
          description.dir = 'auto';
          description.textContent = value;
          fields.append(term, description);
        }
        row.append(fields); list.append(row);
      }
      details.append(summary, list); li.append(details);
    }
    if (check.evidence?.length) {
      li.append(reviewSources(check.evidence, ref => passages.length > 1 && passages.includes(ref) && questionButton(ref)));
    }
    if (check.figures?.length) {
      const figures = document.createElement('ul');
      figures.className = 'review-figures';
      for (const figure of check.figures) {
        const row = document.createElement('li'), name = document.createElement(figure.key ? 'button' : 'span');
        name.textContent = figure.name;
        if (figure.key) {
          name.type = 'button';
          name.onclick = () => openEvidence({key:figure.key, sha256:figure.sha256, label:figure.name}, name);
        }
        const origin = document.createElement('span');
        origin.textContent = rt(figure.origin);
        row.dataset.origin = figure.origin;
        row.append(name, origin); figures.append(row);
      }
      li.append(figures);
    }
    return li;
  }));
  re('review-issues').replaceChildren(...(review.issues || []).map(issue => {
    const li = document.createElement('li'), title = document.createElement('strong'), detail = document.createElement('p'), controls = document.createElement('div');
    li.dataset.status = issue.status || '';
    title.textContent = [issue.id, researchLabels[issue.severity] && rt(issue.severity), issue.claim, researchLabels[issue.status] ? rt(issue.status) : issue.status].filter(Boolean).join(' · ');
    detail.textContent = [issue.reason,issue.fix].filter(Boolean).join(' ');
    controls.className = 'review-controls';
    for (const ref of issue.evidence || []) {
      if (ref.quote) { const quote = document.createElement('p'); quote.textContent = ref.quote; li.append(quote); }
      if (!ref.key) continue;
      const btn = document.createElement('button'); btn.type = 'button';
      btn.textContent = [ref.previous ? rt('before') : rt('after'),ref.artifact,ref.page && rt('page') + ' ' + ref.page].filter(Boolean).join(' · ');
      btn.onclick = () => openEvidence({key:ref.key, sha256:ref.sha256, label:btn.textContent, quote:ref.quote},btn,ref.page,!!preview && preview.opener?.closest('li') === li);
      controls.append(btn);
    }
    const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = rt('steer');
    btn.onclick = () => {
      const input = re('hitl-decision').hidden ? re('hitl-input') : re('decision-text');
      if (!input.value) input.value = [issue.id,issue.claim].join(' · ') + '\n';
      input.dispatchEvent(new Event('input')); input.focus();
    };
    controls.append(btn); li.prepend(title,detail); li.append(controls); return li;
  }));
}
