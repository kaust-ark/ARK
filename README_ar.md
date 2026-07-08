<p align="center">
  <a href="README.md">English</a> &bull; <a href="README_zh.md">中文</a> &bull; <strong>العربية</strong>
</p>

<p align="center">
  <img src="https://idea2paper.org/assets/logo_ark_transparent.png" alt="idea2paper" width="260">
</p>

<h1 align="center">idea2paper</h1>

<p align="center">
  <em>خفف العبء. وجّه العلم.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-6-orange.svg" alt="6 Agents">
  <img src="https://img.shields.io/badge/venues-20+-purple.svg" alt="20+ Venues">
</p>

<p align="center">
  <a href="https://idea2paper.org/"><strong>الموقع الإلكتروني</strong></a> &bull;
  <a href="#بداية-سريعة">بداية سريعة</a> &bull;
  <a href="#المتطلبات">المتطلبات</a> &bull;
  <a href="#مسار-العمل">مسار العمل</a> &bull;
  <a href="#الوكلاء">الوكلاء</a> &bull;
  <a href="#الحوسبة-السحابية">السحابة</a> &bull;
  <a href="#مرجع-cli">واجهة الأوامر</a>
</p>

---

يقوم نظام idea2paper بتنسيق عمل **6 وكلاء ذكاء اصطناعي متخصصين** لتحويل فكرة بحثية إلى ورقة علمية كاملة &mdash; من تحليل المقترح، والبحث في المراجع، وتجارب Slurm، وصولاً إلى صياغة LaTeX والمراجعة العلمية المتكررة &mdash; كل ذلك مع بقائك في موقع التحكم عبر **واجهة الأوامر (CLI)**، أو **لوحة التحكم**، أو **تيليجرام**.

```
أعطه فكرة ومؤتمراً علمياً. وسيتولى idea2paper الباقي.
```

## أوراق بحثية كتبها idea2paper

<table align="center">
<tr>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/marco.pdf"><img src="https://idea2paper.org/assets/paper-marco.png" alt="Budget-Constrained Multi-Modal Research Synthesis" width="320"></a>
<br>
<strong>Budget-Constrained Multi-Modal Research Synthesis via Iterative-Deepening Agentic Search</strong>
<br>
<sub>القالب: EuroMLSys</sub>
</td>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/heteroserve.pdf"><img src="https://idea2paper.org/assets/paper-heteroserve.png" alt="HeteroServe" width="320"></a>
<br>
<strong>HeteroServe: Capability-Weighted Batch Scheduling for Heterogeneous GPU Clusters in LLM Inference</strong>
<br>
<sub>القالب: ICML</sub>
</td>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/tierkv.pdf"><img src="https://idea2paper.org/assets/paper-tierkv.png" alt="TierKV" width="320"></a>
<br>
<strong>TierKV: Prefetch-Aware Memory Tiering for KV Cache in LLM Serving</strong>
<br>
<sub>القالب: NeurIPS</sub>
</td>
</tr>
</table>

---

## بداية سريعة

```bash
curl -fsSL https://idea2paper.org/install.sh | bash
```

السكربت سيقوم بـ:

1. كشف النظام، تثبيت miniforge عند الحاجة، إنشاء بيئتي `ark-base` و `ark`، تثبيت idea2paper بصيغة `pip install -e` داخل `~/ARK`، وتثبيت أدوات Claude Code + Gemini CLI.
2. سؤالك عن: **مفتاح Gemini API**، **رمز Claude OAuth** (`sk-ant-oat01-…` تحصل عليه من `claude /login`)، و**البريد الإلكتروني لتسجيل الدخول إلى لوحة التحكم**. اضغط Enter لتخطّي أي حقل.
3. تثبيت لوحة التحكم كخدمة `systemd --user` على المنفذ `9527` (استخدم `--no-webapp` للتخطّي).
4. طباعة **رابط سحري لمرة واحدة** للبريد المُدخل — انقر عليه مرة واحدة وستدخل لوحة التحكم المحلية. لا حاجة لـ SMTP أو Google OAuth.

بعد ذلك، لوحة التحكم على <http://localhost:9527> هي واجهة العمل الرئيسية — إنشاء المشاريع، اختيار النموذج، التشغيل، والمراقبة. سطر الأوامر يعمل كذلك:

```bash
ark doctor          # التحقّق من التثبيت
ark new myproject   # معالج إنشاء المشروع
ark run  myproject
ark monitor myproject
```

شغّل `ark webapp login <email>` في أي وقت للحصول على رابط دخول جديد. خيارات السكربت الكاملة: [`website/homepage/install.sh --help`](website/homepage/install.sh).

### البدء من ملف PDF موجود

```bash
ark new myproject --from-pdf proposal.pdf
```

يقوم idea2paper بتحليل ملف PDF باستخدام PyMuPDF + Claude Haiku، ويملأ البيانات تلقائياً، ويبدأ من المواصفات المستخرجة.

---

## المتطلبات

- **Python 3.10+** مع `pyyaml` و `PyMuPDF`
- **واجهة أوامر الوكيل**: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (موصى به، باقة Claude Max)، [OpenAI Codex](https://github.com/openai/codex)، **أو** [Gemini CLI](https://github.com/google-gemini/gemini-cli) &mdash; قابل للاختيار لكل مشروع.
- **اختياري**: LaTeX (`pdflatex` + `bibtex`)، Slurm، `google-genai` للأشكال البيانية المولدة بالذكاء الاصطناعي.

### التثبيت

أسرع طريقة هي السكربت ذو الأمر الواحد المذكور في [بداية سريعة](#بداية-سريعة)، فهو يقوم بهذه الخطوات نيابة عنك. للتنفيذ اليدوي:

```bash
# 1. أنشئ قالب بيئة المشاريع (المكدّس البحثي فقط، بدون كود idea2paper —
#    كل مشروع جديد يستنسخ هذه البيئة، لذلك يجب أن تبقى نظيفة).
conda env create -f environment.yml         # لأنظمة Linux (ينشئ "ark-base")
# أو لنظام macOS:
conda env create -f environment-macos.yml   # لأنظمة macOS (ينشئ "ark-base")

# 2. ثبّت idea2paper نفسه في بيئة منفصلة (ليست ark-base).
conda create -n ark python=3.11 -y
conda activate ark
pip install -e .                    # النواة الأساسية
pip install -e ".[research]"       # + Gemini Deep Research و Nano Banana
pip install -e ".[webapp]"         # + دعم لوحة التحكم وخدمة systemd

# 3. تحقّق
ark doctor
```

---

## إطار العمل

<p align="center">
  <img src="assets/framework.png" alt="idea2paper framework" width="900">
</p>

ينسق idea2paper ثلاث مراحل &mdash; **التهيئة والبحث**، **التطوير المتكرر**، و **المراجعة المتكررة** &mdash; من خلال ذاكرة مشتركة، و**Goal Anchor** ثابت يُحقن من جديد في كل استدعاء وكيل لمنع الانحراف عبر التكرارات، إضافةً إلى تدخل بشري عبر لوحة التحكم أو تيليجرام.

---

## مسار العمل

ينفذ idea2paper ثلاث مراحل بالتتابع. وتتكرر مرحلة المراجعة حتى تصل الورقة إلى الدرجة المستهدفة.

| المرحلة | ماذا يحدث |
|:------|:-------------|
| **البحث** | مسار من 5 خطوات: الإعداد (بيئة conda) &larr; تحليل المقترح (researcher) &larr; بحث عميق (Gemini) &larr; التخصيص (researcher) &larr; التمهيد (المهارات والاستشهادات) |
| **التطوير** | دورة تجارب متكررة: تخطيط &larr; تشغيل على Slurm &larr; تحليل &larr; كتابة المسودة الأولى |
| **المراجعة** | تجميع &larr; مراجعة &larr; تخطيط &larr; تنفيذ &larr; تحقق، مع التكرار حتى تصل الدرجة &ge; العتبة المطلوبة |

### حلقة المراجعة

كل تكرار لمرحلة المراجعة يتكون من **5 خطوات**:

| الخطوة | الوصف |
|:-----|:------------|
| **التجميع** | تحويل LaTeX إلى PDF، حساب عدد الصفحات، وصور الصفحات |
| **المراجعة** | مراجع ذكاء اصطناعي يعطي درجة من 1-10، ويسرد القضايا الكبرى والصغرى |
| **التخطيط** | المخطط (Planner) ينشئ خطة عمل ذات أولوية |
| **التنفيذ** | الباحث والمجرب يعملان بالتوازي؛ والكاتب يراجع نصوص LaTeX |
| **التحقق** | التأكد من نجاح تجميع التعديلات؛ وإعادة إنشاء ملف PDF |

تتكرر الحلقة حتى تصل الدرجة إلى عتبة القبول &mdash; أو تتدخل أنت عبر تيليجرام.

---

## الوكلاء

| الوكيل | الدور |
|:------|:-----|
| **الباحث** | يحلل المقترح؛ يجري مسحاً أدبياً مدعوماً بـ Gemini؛ ويخصص مطالبات الوكلاء للمشروع |
| **المراجع** | يقيم الورقة وفقاً لمعايير المؤتمر، ويولد مهام التحسين |
| **المخطط** | يحول ملاحظات المراجعة إلى خطة عمل؛ يحلل نتائج مرحلة التطوير |
| **الكاتب** | يصيغ ويحسن أقسام LaTeX مع استشهادات موثقة من DBLP |
| **المجرب** | يصمم التجارب، يرسل وظائف Slurm، ويحلل النتائج |
| **المبرمج** | يكتب ويصحح أكواد التجارب وسكربتات التحليل |

---

## ما الذي يميز idea2paper

| | أدوات أخرى | idea2paper |
|---|:------------|:----|
| **التحكم** | استقلالية كاملة &mdash; انحراف عن القصد، لا تصحيح أثناء التشغيل | تدخل بشري: توقف عند القرارات الرئيسية، توجيه عبر تيليجرام أو الويب |
| **التنسيق** | تخطيطات مكسورة، أخطاء LaTeX، تنظيف يدوي | قوالب المؤتمرات + تحكم دون مستوى الصفحة للالتزام بالحد الأقصى للصفحات بدقة |
| **الاستشهاد** | النماذج اللغوية تبتكر مراجع وهمية | BibTeX من واجهات برمجية أولاً (DBLP / CrossRef / arXiv) مع مواءمة المحتوى مع الادعاء |
| **المراجعة** | مراجعة نصية فقط لمصدر LaTeX | مراجعة بصرية: صور الصفحات **و** المصدر، بتقييم وفق معايير المؤتمر |
| **الأشكال** | أنماط افتراضية، أحجام خاطئة، لا وعي بالصفحة | Nano Banana + قماش واعي بالمكان، عرض الأعمدة، والخطوط |
| **العزل** | بيئة مشتركة &mdash; المشاريع تتداخل مع بعضها | بيئة conda لكل مشروع، HOME معزول، عزل كامل للمستأجرين |
| **النزاهة** | المحاكاة بدلاً من التجارب الحقيقية | مطالبات تمنع المحاكاة + مهارات مدمجة تفرض التنفيذ الحقيقي |

---

## عزل البيئة

يعمل كل مشروع في **بيئة conda خاصة به**، يتم استنساخها من بيئة أساسية عند إنشاء المشروع. وهذا يضمن عزلاً كاملاً:

- **Python معزول** &mdash; دليل `.env/` خاص لكل مشروع مع حزمه الخاصة.
- **HOME معزول** &mdash; يعمل كل منسق مع ضبط `HOME` على دليل المشروع.
- **لا تلوث متبادل** &mdash; تمنع `PYTHONNOUSERSITE=1` تسرب حزم المستخدم العامة.
- **تجهيز تلقائي** &mdash; يكتشف `ark run` وبوابة الويب بيئة المشروع ويستخدمانها؛ ويقوم المسار بتهيئتها إذا كانت مفقودة.

```bash
# يتم إنشاء بيئة conda تلقائياً عند أول تشغيل.
# ark run سيكتشفها ويستخدمها:
ark run myproject
#   Conda env: /path/to/projects/myproject/.env
```

## نظام المهارات

يأتي idea2paper مع **مهارات مدمجة** &mdash; مجموعات تعليمات برمجية يحملها الوكلاء لفرض أفضل الممارسات:

| المهارة | الغرض |
|:------|:--------|
| **نزاهة البحث** | تمنع المحاكاة: يجب على الوكلاء تشغيل تجارب حقيقية |
| **التدخل البشري** | بروتوكول التصعيد: يتوقف الوكلاء للسؤال قبل الإجراءات غير القابلة للتراجع |
| **عزل البيئة** | يفرض حدود البيئة الخاصة بكل مشروع |
| **صندوق وقت التشغيل** | يقيّد كل مشروع وقت التشغيل ببيئة conda الخاصة به و`HOME` ودليل مؤقت خاص |
| **نزاهة الأشكال البيانية** | يتحقق من مطابقة الأشكال للبيانات؛ يمنع الرسوم الوهمية |
| **تعديل الصفحات** | يحافظ على حدود عدد الصفحات عبر تعديل كثافة المحتوى |

تعيش المهارات المدمجة في `skills/builtin/` ويتم تثبيتها تلقائياً أثناء تهيئة المسار. وتقع مهارات المجالات (مثل HPC) في `skills/library/`، ويختارها الباحث عند الحاجة.

---

## مرجع CLI

| الأمر | الوصف |
|:--------|:------------|
| `ark new <name>` | إنشاء مشروع عبر معالج تفاعلي |
| `ark run <name>` | إطلاق مسار العمل (يكتشف بيئة المشروع تلقائياً) |
| `ark status [name]` | الدرجة، التكرار، المرحلة، التكلفة |
| `ark monitor <name>` | لوحة مراقبة حية: نشاط الوكلاء، اتجاه الدرجة |
| `ark update <name>` | حقن تعليمات أثناء التشغيل |
| `ark stop <name>` | إيقاف هادئ |
| `ark restart <name>` | إيقاف + إعادة تشغيل |
| `ark research <name>` | تشغيل Gemini Deep Research بشكل مستقل |
| `ark config <name> [key] [val]` | عرض أو تحرير الإعدادات |
| `ark clear <name>` | إعادة ضبط الحالة لبداية جديدة |
| `ark delete <name>` | حذف المشروع تماماً |
| `ark setup-bot` | إعداد بوت تيليجرام |
| `ark list` | سرد جميع المشاريع مع حالتها |
| `ark doctor` | تشخيص التثبيت الذاتي (البيئات، مفاتيح API، الويب) |
| `ark cite-check <name>` | التحقق من استشهادات المشروع عبر DBLP / CrossRef |
| `ark cite-search <query>` | البحث في قواعد البيانات الأكاديمية |
| `ark webapp install` | تثبيت خدمة لوحة التحكم |
| `ark access {list,add,remove,add-domain,remove-domain}` | إدارة قائمة Cloudflare Access الخاصة بلوحة التحكم |

---

## لوحة التحكم (Dashboard)

يتضمن idea2paper لوحة تحكم قائمة على الويب لإدارة المشاريع وتوجيه الوكلاء. تعرض اللوحة **شارات المراحل الحية** (Research / Dev / Review)، وتتبع التكاليف في الوقت الفعلي. تدار الخدمة عبر عملية FastAPI واحدة &mdash; منفذ واحد، وحدة systemd واحدة.

### الإعدادات

يُضبط الإعداد عبر `.ark/webapp.env` (يُنشأ تلقائياً عند أول تشغيل لـ `ark webapp`). اضبط `SMTP_*` لتفعيل تسجيل الدخول عبر الرابط السحري، واستخدم `ALLOWED_EMAILS` / `EMAIL_DOMAINS` لتقييد الوصول، واختيارياً `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` لتفعيل Google OAuth.

### أوامر الإدارة

| الأمر | الوصف |
|:--------|:------------|
| `ark webapp` | بدء لوحة التحكم في المقدمة (مفيد للتصحيح). |
| `ark webapp release` | وسم الكود الحالي ونشره في بيئة الإنتاج. |
| `ark webapp install [--dev]` | تثبيت والبدء كخدمة `systemd` للمستخدم. |
| `ark webapp status` | عرض حالة خدمة systemd. |
| `ark webapp restart` | إعادة تشغيل خدمة لوحة التحكم. |
| `ark webapp logs [-f]` | عرض أو تتبع سجلات الخدمة. |

<details>
<summary><strong>تفاصيل الخدمة (الإنتاج vs التطوير)</strong></summary>

| | الإنتاج | التطوير |
|---|:-----|:----|
| **المنفذ** | 9527 | 1027 |
| **اسم الخدمة** | `ark-webapp` | `ark-webapp-dev` |
| **بيئة Conda** | `ark-prod` | `ark-dev` |
| **مصدر الكود** | `~/.ark/prod/` (مثبت) | المستودع الحالي (مباشر) |

</details>

### النشر الجماعي (prod مشترك)

يمكن للفريق تشغيل نسخة إنتاج واحدة من مجلد مشترك قابل للكتابة من قبل المجموعة، بينما يطوّر كل عضو في نسخته الخاصة وينشر بأمر واحد. في ملف shell rc لكل عضو:

```bash
export ARK_RELEASE_ROOT=/shared/path/ARK    # النسخة المشتركة: prod worktree وقاعدة البيانات والمشاريع
export ARK_CONDA_ROOT=/shared/path/conda    # conda المشترك (بيئتا ark-prod / ark-base)
export ARK_TOOLS_BIN=/shared/path/tools/bin # OpenHands CLI المشترك
umask 002                                   # إبقاء الملفات الجديدة قابلة للكتابة من المجموعة
```

بعد الإعداد، يكفي أن يشغّل **أي عضو** الأمر `ark webapp release` من نسخته: يوسم الإصدار ويدفعه ويحدّث prod worktree المشترك ويثبّت في البيئة المشتركة؛ ثم يلاحظ تطبيق الويب تغيّر علامة `.deployed-tag` ويعيد تشغيل نفسه خلال ~30 ثانية — دون الحاجة لامتلاك الخدمة.

<details>
<summary><strong>الاستدعاء المباشر للمنسق</strong></summary>

```bash
python -m ark.orchestrator --project myproject --mode paper --max-iterations 20
python -m ark.orchestrator --project myproject --mode dev
```

</details>

---

## استخدام Docker

### متطلبات المعمارية

> [!IMPORTANT]
> يعتمد وقت تشغيل idea2paper على مكتبات علمية تكون أكثر استقراراً على x86_64. إذا كنت تستخدم **Apple Silicon (M1/M2/M3)**، يجب عليك البناء لمنصة `linux/amd64`.
>
> جميع ملفات idea2paper Dockerfiles و `docker-compose.yml` مضبوطة لفرض `linux/amd64` افتراضياً.

### التشغيل عبر Docker Compose

أسهل طريقة لتشغيل بوابة idea2paper هي استخدام `docker-compose`. من جذر المشروع:

```bash
# بدء البوابة (يبني الصورة تلقائياً لـ amd64)
docker compose -f docker/docker-compose.yml up --build -d
```

ستكون البوابة متاحة على `http://localhost:9527`. يتم حفظ جميع قواعد البيانات والإعدادات والمشاريع تلقائياً في Docker volume باسم `ark_data`.

لعرض السجلات المباشرة:
```bash
docker compose -f docker/docker-compose.yml logs -f webapp
```

### الرفع إلى منصة Google Cloud (GCP)

يتضمن idea2paper سكربت لبناء ورفع الصور إلى Google Artifact Registry أو GCR.

```bash
# الرفع إلى Artifact Registry (موصى به)
./docker/push-gcp.sh --project [PROJECT_ID] --region [REGION] --repo [REPO] --build

# الرفع إلى Container Registry القديم (gcr.io)
./docker/push-gcp.sh --project [PROJECT_ID] --legacy --build
```
يعمل خيار `--build` تلقائياً على بناء الصور لمعمارية `linux/amd64` حتى عند التشغيل على macOS.

---

## الحوسبة السحابية (Cloud Compute)

تفصل **بنية السحابة v2** في idea2paper بين *مستوى التحكم (Control Plane)* و*مستوى التنفيذ (Execution Plane)*، مما يتيح تشغيل المنسق الكامل على **عنقود مُجهَّز عبر SkyPilot** بينما تتفاعل أنت مع تطبيق ويب محلي خفيف. أصبح [SkyPilot](https://docs.skypilot.co) الآن المسار الوحيد للحوسبة السحابية — فهو يُجهِّز الموارد عبر AWS/GCP/Azure/Kubernetes من تجريد واحد، مع مثيلات spot، وإعادة المحاولات، والإنهاء التلقائي (autostop) المدمج.

**كيف يعمل:**
1. يعمل تطبيق الويب المحلي (أو CLI) كمُطلِق خفيف — فهو يشغّل `sky launch` لتجهيز **عنقود المنسق (Orchestrator)** البعيد، ويزامن كود مشروعك (عبر `workdir`/`file_mounts` الخاصة بـ SkyPilot) ومفاتيح API، ثم يشغّل عملية المنسق.
2. يشغّل **عنقود المنسق** كل المنطق عالي المستوى (الباحث، المخطِّط، الكاتب، LaTeX، الأشكال) عن بُعد في جلسة منفصلة.
3. يمكن تشغيل التجارب على عنقود المنسق نفسه أو على عنقود GPU منفصل مُجهَّز عبر SkyPilot (قابل للضبط بشكل مستقل).
4. يبلّغ المنسق عن حالته إلى الديار عبر واجهة `/v1` لمستوى التحكم؛ ويبثّ تطبيق الويب السجلات ويحدّث لوحة التحكم. يُنهي العنقود نفسه ذاتياً عبر **autostop** عند اكتمال التشغيل (أو بعد فترة خمول).

> [!TIP]
> يتم تشفير بيانات اعتماد السحابة في حالة السكون باستخدام `SECRET_KEY` الخاص بك. لا يتم تسجيل مفاتيحك أو إرسالها إلى أطراف ثالثة.

<details>
<summary><strong>تسلسل الإعدادات الهرمي (Configuration Hierarchy)</strong></summary>

يستخدم idea2paper نموذج إعدادات من ثلاث طبقات للحوسبة السحابية:
1. **الإعدادات الافتراضية للنظام**: تُضبط في `webapp.env` (المتغير المركزي `CLOUD_GCP_PROJECT` الذي يحوي صورة ARK المخبوزة، إضافة إلى `CLOUD_LAUNCHER_SA` و`CLOUD_LAUNCHER_SA_KEY` و`CLOUD_CONDA_ENV`).
2. **الإعدادات الافتراضية العامة للمستخدم**: تُضبط في لوحة **Settings** (⚙️). وتنطبق على كل مشاريعك.
3. **تجاوزات المشروع**: تُضبط أثناء إنشاء المشروع أو إعادة تشغيله. ولها الأولوية القصوى.

يتيح لك هذا التسلسل تعريف إعدادات افتراضية قياسية مرة واحدة، مع سهولة التبديل إلى مثيل GPU قوي (accelerators، spot) لتجربة محددة.

</details>

---

### تفعيل الحوسبة السحابية عبر لوحة التحكم

1. افتح لوحة **Settings** (أيقونة ⚙️ في شريط التنقل العلوي).
2. افتح تبويب **Compute**.
3. أدخل **GCP Project ID** الخاص بك، وامنح حساب الخدمة المعروض `ark-launcher` الأدوار المطلوبة على مشروعك، ثم انقر **Verify access**. (لا يتم رفع أي مفتاح لحساب خدمة — بل تفوّض الوصول إلى مشروعك عبر IAM. راجع كتلة إعداد GCP أدناه.)
4. انقر **Save**.

عند إنشاء مشروع جديد يمكنك الآن الاختيار بشكل مستقل:
- **Orchestrator Backend** — `skypilot` لتشغيل مستوى التحكم على عنقود مُجهَّز عبر SkyPilot، أو `local` لتشغيله على نفس جهاز تطبيق الويب.
- **Experiment Backend** — `skypilot` لتجارب GPU، أو `local` لتشغيلها على عنقود المنسق نفسه.

---

<details>
<summary><strong>إنشاء مشروع</strong></summary>

بمجرد ضبط الحوسبة السحابية، أطلق مشروعاً عبر لوحة التحكم:

1. انقر **New Project** من الصفحة الرئيسية للوحة التحكم.
2. املأ هدف البحث، والمؤتمر المستهدف، وأي تعليمات إضافية.
3. انقر **Submit** — يولّد تطبيق الويب ملف `config.yaml`، ويجهّز عنقود المنسق عبر SkyPilot، ويزامن مشروعك، ويبدأ التشغيل.

يُخزَّن ملف `config.yaml` المُولَّد في:

```
~/.ark/data/projects/<user_id>/<project_id>/config.yaml
```

يمكنك فحص هذا الملف أو تحريره يدوياً في أي وقت (مثلاً لضبط نوع المثيل أو إضافة `setup_commands`). تسري التغييرات في التشغيل أو إعادة التشغيل التالي.

> [!NOTE]
> إذا كان `PROJECTS_ROOT` مضبوطاً في `.ark/webapp.env`، فسيُستبدل المسار أعلاه بـ `$PROJECTS_ROOT/<user_id>/<project_id>/config.yaml`.

</details>

---

### إعداد مزودي السحاب

<details>
<summary><strong>☁️ Google Cloud Platform (GCP) — عبر SkyPilot</strong></summary>

إعداد GCP بلا مفاتيح: بدلاً من رفع مفتاح حساب خدمة، تمنح حساب الخدمة المركزي **`ark-launcher`** الخاص بـ idea2paper وصولاً إلى مشروع GCP *الخاص بك* عبر IAM. عندها يشغّل SkyPilot العناقيد في مشروعك من صورة ARK المخبوزة مسبقاً.

#### 1. تفعيل الواجهات البرمجية المطلوبة

```bash
export PROJECT_ID=your-gcp-project-id
gcloud services enable compute.googleapis.com --project=$PROJECT_ID
```

#### 2. بناء صورة الجهاز

يبدأ idea2paper من صورة GCP مخبوزة مسبقاً تحوي كل تبعيات النظام (Conda، LaTeX، Node.js) لبدء تشغيل سريع. يطلق SkyPilot من هذه الصورة. ابنِها مرة واحدة:

```bash
./scripts/build_ark_gcp_image.sh [GCP_PROJECT_ID] [ZONE]
```

يشغّل هذا السكربت جهازاً افتراضياً مؤقتاً، ويثبّت TeX Live وMiniforge وNode.js وبيئة `ark-base`، ثم يحفظ Machine Image باسم `ark-job-v1-[timestamp]` موسومة بعائلة `ark-job`.

> في نشر مُستضاف، يبني المشغّل هذه الصورة مرة واحدة في المشروع المركزي `CLOUD_GCP_PROJECT` (إعداد الخادم: ينشئ `scripts/setup_ark_launcher_sa.sh` حساب خدمة المُطلِق المركزي؛ ويخبز `scripts/build_ark_gcp_image.sh` الصورة). أما المستضيفون الذاتيون فيشغّلون كلا السكربتين في مشروعهم الخاص.

#### 3. منح حساب خدمة المُطلِق والتحقق

في لوحة التحكم، افتح **Settings → Compute**:
1. أدخل **GCP Project ID** الخاص بك.
2. امنح حساب الخدمة المعروض `ark-launcher` أدوار IAM المطلوبة على مشروعك **أنت** (تسرد اللوحة أوامر `gcloud ... add-iam-policy-binding` الدقيقة للتشغيل).
3. انقر **Verify access**.

لا يغادر أي مفتاح Google أبداً — بل تفوّض وصولاً محدود النطاق إلى مشروعك. راجع [`SKYPILOT_PLAN.md`](SKYPILOT_PLAN.md) لتصميم تعدد المستأجرين.

#### 4. مرجع `config.yaml` (متقدم / CLI فقط)

يولّد تطبيق الويب هذا تلقائياً من إعداداتك. للمشاريع اليدوية أو المُدارة عبر CLI، راجع [`config.example.yaml`](config.example.yaml) للقالب الكامل. النموذج + المفاتيح (كل الوكلاء يعملون عبر OpenHands → LiteLLM):

```yaml
model: anthropic/claude-sonnet-4-6     # الوكلاء يشغّلون هذا — أي نموذج LiteLLM
                                       # (gemini/… , openai/… , deepseek/… , …)
bot_model: anthropic/claude-haiku-4-5  # نموذج رخيص للمساعدات الخفيفة (العناوين، الملخصات)
anthropic_api_key: "sk-ant-..."        # املأ المزود(ين) الذي تستخدمه — بادئة النموذج
openai_api_key:    "sk-..."            #   هي ما يحدد أي مفتاح يُستخدم
gemini_api_key:    "..."               # مفتاح gemini يشغّل أيضاً Deep Research (اختياري)
```

تستخدم الحوسبة خلفيتَي SkyPilot (عنقود المنسق + عنقود التجارب) مثبّتتين على GCP:

```yaml
# عنقود المنسق: يشغّل الباحث، المخطِّط، الكاتب، LaTeX (لا حاجة لـ GPU)
orchestrator_compute_backend:
  type: skypilot
  cloud: gcp
  # region: us-central1
  # instance_type: n4-standard-2
  # idle_minutes_to_autostop: 60     # شبكة أمان عند الأعطال: DOWN تلقائي عند الخمول

# عنقود التجارب: يشغّل الأحمال كثيفة الاستخدام لـ GPU
experiment_compute_backend:
  type: skypilot
  cloud: gcp
  accelerators: L4:1                  # مواصفة مسرّع SkyPilot ("<NAME>:<COUNT>")
  use_spot: true                      # مثيلات أرخص قابلة للاستباق (pre-emptible)
  setup_commands:
    - pip install -r requirements.txt
```

> لتشغيل التجارب على عنقود المنسق بدلاً من عنقود منفصل، اضبط `experiment_compute_backend.type: local`. راجع كتلة SkyPilot أدناه لمرجع المفاتيح الكامل.

</details>

---

> تشغيل على **AWS أو Azure أو Kubernetes** (أو تريد أن يختار SkyPilot أرخص سحابة تلقائياً)؟ استخدم كتلة SkyPilot أدناه واضبط بيانات اعتماد تلك السحابة وفق توثيق SkyPilot على [https://docs.skypilot.co](https://docs.skypilot.co).

<details>
<summary><strong>☁️ SkyPilot (عبر السحابات و Kubernetes)</strong></summary>

يُجهِّز [SkyPilot](https://github.com/skypilot-org/skypilot) الأجهزة الافتراضية عبر
AWS/GCP/Azure (و Kubernetes) من تجريد واحد، مع spot وإعادة المحاولات والإنهاء
التلقائي (autostop) المدمج — بحيث تغطي كتلة `type: skypilot` واحدة الحالة أحادية
السحابة و Kubernetes الخاص بك دون خلفية منفصلة لكل سحابة. إنه المسار السحابي الوحيد
لـ idea2paper: كل من خلفية **التجارب** (الطبقة 1) ومُطلِق **المنسق** (الطبقة 2)
يُجهِّزان عبر SkyPilot، مع تطبيق **autostop-down** لأمان التكلفة عند الإطلاق
(راجع `idle_minutes_to_autostop` أدناه).

#### الإعداد

```bash
# ثبّت SkyPilot مع السحابات التي تستخدمها (راجع توثيق SkyPilot للإعداد الكامل)
pip install 'ark[skypilot]'
pip install 'skypilot[gcp,aws,kubernetes]'
sky check          # تحقق من قدرة SkyPilot على الوصول إلى سحاباتك/عناقيدك المضبوطة
```

#### مرجع `config.yaml` (متقدم / CLI فقط)

> مفاتيح **`experiment_compute_backend`** أدناه تُحلَّل بواسطة خلفية الطبقة 1؛
> أما كتلة **`orchestrator_compute_backend`** فتُقرأ بواسطة `SkyPilotVmJobLauncher`
> في الطبقة 2. وكلاهما يتشاركان نفس مفاتيح الموارد (`cloud` / `region` /
> `accelerators` / `instance_type` / `use_spot` / `disk_size` / `image_id` /
> `cluster_name` / `setup_commands` / `idle_minutes_to_autostop`).

```yaml
# تجارب مُجهَّزة عبر SkyPilot. كل مفتاح عدا `type` اختياري؛
# يختار SkyPilot أرخص سحابة/عنقود يمكن الوصول إليه ما لم تثبّت واحداً.
experiment_compute_backend:
  type: skypilot
  # cloud: aws                   # aws | gcp | azure | kubernetes؛ احذفه → تلقائي
  # region: us-east-1            # تثبيت منطقة اختياري
  accelerators: L4:1             # مواصفة مسرّع SkyPilot ("<NAME>:<COUNT>")
  # instance_type: g5.xlarge     # نوع مثيل صريح اختياري
  use_spot: true                 # مثيلات أرخص قابلة للاستباق (pre-emptible)
  # disk_size: 256               # جيجابايت، اختياري
  # cluster_name: ark-myproj     # اختياري؛ الافتراضي ark-<project>
  # idle_minutes_to_autostop: 60 # DOWN تلقائي بعد N دقيقة خمول (أمان التكلفة)؛
  #                              # عناقيد التجارب تُنهى دائماً تلقائياً — هذا
  #                              # يضبط النافذة فقط، ولا يمكن تعطيله
  #                              # (وإلا لن يستطيع مستوى التحكم حصادها).
  setup_commands:                # تُثبَّت التبعيات عبر كتلة setup: في SkyPilot
    - pip install -r requirements.txt

# مُطلِق المنسق — يشغّل `python -m ark.orchestrator` على عنقود SkyPilot.
# يبلّغ إلى الديار عبر واجهة /v1 لمستوى التحكم (اضبط control_plane_url)، لذا
# لا يحتاج العنقود إلى نظام ملفات/قاعدة بيانات مشتركة مع لوحة التحكم.
orchestrator_compute_backend:
  type: skypilot
  # cloud: gcp                     # احذفه → يختار SkyPilot تلقائياً
  # region: us-central1
  # instance_type: n1-standard-2
  # cluster_name: ark-orch-myproj  # اختياري؛ الافتراضي ark-orch-<project>
  # idle_minutes_to_autostop: 60   # شبكة أمان عند الأعطال: DOWN تلقائي بعد N دقيقة خمول
  #                                # بعد خروج مهمة المنسق. اضبط `off`
  #                                # للتعطيل، أو `autostop_down: false` للإيقاف STOP
  #                                # (مع إبقاء القرص) بدلاً من الإنهاء.
  setup_commands:                  # ثبّت تبعيات ARK على العنقود (workdir →
    - cd ~/sky_workdir && pip install -e '.[research]'   # ~/sky_workdir عند الإطلاق)
```

> تملأ لوحة التحكم كتلة `setup:` هذه تلقائياً؛ فقط مستخدمو CLI الذين يحررون
> `config.yaml` يدوياً بحاجة لضبطها. تثبّت مصدر ARK المُزامَن مع إضافة `research`
> كي يُحلَّل `python -m ark.orchestrator` على عنقود عارٍ (يمكن لصورة `image_id`
> مخبوزة أن تحل محلها لاحقاً لسرعة الإطلاق فقط — اضبط المفتاح وأسقط `pip install`).
> موارد تجارب الطبقة 1 (`region` / `instance_type` / `image_id`) **لا** تُشتق
> تلقائياً من كتلة المنسق — فهي تعيش في فضاء أسماء SkyPilot مختلف، لذا اضبطها هنا صراحةً.

> لا يمكن لمنسق `skypilot` تشغيل تجارب `slurm` — فالمنسق السحابي ليس له مسار شبكي
> إلى عنقود SLURM محلي.

</details>

---

<details>
<summary><strong>بثّ السجلات وإعادة الارتباط</strong></summary>

- **بثّ السجلات** — يبثّ عنقود المنسق السجلات إلى الديار عبر واجهة `/v1` لمستوى التحكم؛ ويستقصيها تطبيق الويب دورياً لعرض التقدم الحي.
- **مزامنة الحالة** — يحفظ المنسق نقاط تفتيش لحالة `auto_research/` إلى مستوى التحكم دورياً، فتبقى واجهة لوحة التحكم محدَّثة ويبقى التشغيل صامداً عند فقدان الـ VM.
- **إعادة الارتباط** — إذا أعدت تشغيل تطبيق الويب المحلي، يكتشف idea2paper عنقود SkyPilot المُخزَّن ويعيد الارتباط بالعملية الجارية دون إعادة تجهيز.

</details>

<details>
<summary><strong>التحكم في التكاليف</strong></summary>

> [!WARNING]
> تُحاسَب العناقيد السحابية بالساعة. يعتمد idea2paper على الإنهاء المدمج في SkyPilot لمنع التكاليف الجامحة:
>
> - **Autostop-down** — يُطلَق كل عنقود بنافذة autostop-down؛ فإذا بقي خاملاً بعد تلك النافذة **أنهى نفسه**. عناقيد التجارب *تُنهى دائماً* تلقائياً (يمكن ضبطها فقط، لا تعطيلها)؛ أما عناقيد المنسق فتفترض نافذة كشبكة أمان عند الأعطال (قابلة للضبط عبر `idle_minutes_to_autostop`).
> - **الإيقاف اليدوي** — النقر على **Stop** في لوحة التحكم يُفرِّغ النتائج ويُنهي العنقود (`sky down`).
>
> إذا قُتلت عملية تطبيق الويب فجأة، فإن نافذة autostop-down تُنهي العنقود بنفسها. تحقق دائماً من عدم بقاء عناقيد شاردة (`sky status`) بعد الإغلاق غير المتوقع.

</details>

---

## تيليجرام (Telegram Integration)

```bash
ark setup-bot    # لمرة واحدة: الصق توكن BotFather، وسيتم اكتشاف chat ID تلقائياً
```

ما ستحصل عليه:
- **تنبيهات غنية** &mdash; تغيرات الدرجات، انتقالات المراحل، نشاط الوكلاء، والأخطاء.
- **إرسال تعليمات** &mdash; توجيه التكرار الحالي في الوقت الفعلي.
- **طلب ملفات PDF** &mdash; إرسال أحدث نسخة من الورقة للمحادثة.
- **التدخل البشري** &mdash; يتوقف الوكلاء للسؤال قبل الإجراءات غير القابلة للتراجع.

---

## المؤتمرات العلمية المدعومة

تأتي قوالب LaTeX جاهزة لـ **NeurIPS وICML وICLR وAAAI وMLSys**، وعائلة **ACL** (ACL / EMNLP / NAACL / EACL / AACL / COLING)، وعائلة **CVF** (CVPR / ICCV / WACV)، وعائلة **ACM acmart** (SOSP / EuroSys / ASPLOS / EuroMLSys)، وعائلة **USENIX** (OSDI / NSDI / ATC / FAST / Security)، و**IEEE / IEEEtran** (INFOCOM وغيرها من مؤتمرات IEEE) &mdash; جميعها من ملفات الأنماط الرسمية لعام 2026 (يستخدم MLSys حزمة 2025 دون تغيير) &mdash; إضافةً إلى قالب **article** عام لـ TMLR وورش العمل والتقارير التقنية. كما يُقبل استخدام قوالب مخصّصة &mdash; إذ يفحص idea2paper ملفات `.tex` / `.aux` / `.sty` لاستيعاب التنسيق، ويصلح أخطاء التجميع، ويضبط حد الصفحات بدقة.

## المجتمع

<p align="center">
  <strong>微信交流群 / Join our WeChat group</strong><br>
  <img src="assets/wechat_qr.jpg" alt="WeChat group: Idea2Paper" width="240"><br>
  <sub>يتم تحديث رمز QR لمجموعة WeChat دوريًا — افتح issue إذا انتهت صلاحيته.</sub>
</p>

## الترخيص

[Apache 2.0](LICENSE)
