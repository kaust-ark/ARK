<p align="center">
  <a href="README.md">English</a> &bull; <a href="README_zh.md">中文</a> &bull; <strong>العربية</strong>
</p>

<p align="center">
  <img src="https://idea2paper.org/assets/logo_ark_transparent.png" alt="ARK" width="260">
</p>

<h1 align="center">ARK &mdash; مجموعة أدوات البحث الآلي</h1>

<p align="center">
  <em>أتمتة العمل الشاق، لا الاتجاه.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-6-orange.svg" alt="6 وكلاء">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="+11 مؤتمر">
  <img src="https://img.shields.io/badge/tests-114-brightgreen.svg" alt="114 اختبار">
</p>

<p align="center">
  <a href="https://idea2paper.org/"><strong>الموقع</strong></a> &bull;
  <a href="#البداية-السريعة">البداية السريعة</a> &bull;
  <a href="#ark-pipeline">Pipeline</a> &bull;
  <a href="#ark-agents">Agents</a> &bull;
  <a href="#مرجع-الأوامر">الأوامر</a>
</p>

---

يُنسّق ARK **٦ وكلاء ذكاء اصطناعي متخصصين** لتحويل فكرة بحثية إلى ورقة علمية — تحليل المقترح، بحث أدبي، تجارب Slurm، صياغة LaTeX، ومراجعة تكرارية — بينما تبقى في السيطرة عبر **CLI** أو **لوحة التحكم** أو **Telegram**.

```
قدّم فكرة ومؤتمرًا مستهدفًا. ARK يتولى الباقي.
```

## أوراق بحثية كتبها ARK

<table align="center">
<tr>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/marco.pdf"><img src="https://idea2paper.org/assets/paper-marco.png" alt="MARCO" width="320"></a>
<br>
<strong>MARCO: Budget-Constrained Multi-Modal Research Synthesis via Iterative-Deepening Agentic Search</strong>
<br>
<sub>قالب: EuroMLSys</sub>
</td>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/heteroserve.pdf"><img src="https://idea2paper.org/assets/paper-heteroserve.png" alt="HeteroServe" width="320"></a>
<br>
<strong>HeteroServe: Capability-Weighted Batch Scheduling for Heterogeneous GPU Clusters in LLM Inference</strong>
<br>
<sub>قالب: ICML</sub>
</td>
</tr>
<tr>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/tierkv.pdf"><img src="https://idea2paper.org/assets/paper-tierkv.png" alt="TierKV" width="320"></a>
<br>
<strong>TierKV: Prefetch-Aware Memory Tiering for KV Cache in LLM Serving</strong>
<br>
<sub>قالب: NeurIPS</sub>
</td>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/gac.pdf"><img src="https://idea2paper.org/assets/paper-gac.png" alt="GAC" width="320"></a>
<br>
<strong>Why Smaller Is Slower: Dimensional Misalignment in Compressed Large Language Models</strong>
<br>
<sub>قالب: ICLR</sub>
</td>
</tr>
</table>

---

## ARK Pipeline

يمرّ ARK بثلاث مراحل متتابعة. مرحلة المراجعة تتكرر حتى تصل الورقة إلى الدرجة المستهدفة.

<p align="center">
  <img src="https://idea2paper.org/assets/pipeline_overview.png" alt="ARK Pipeline" width="700">
</p>

| المرحلة | ما يحدث |
|:--------|:--------|
| **Research** | خط أنابيب من ٥ خطوات: إعداد البيئة (conda) &rarr; تحليل المقترح (الباحث) &rarr; Deep Research (Gemini) &rarr; التخصيص (الباحث) &rarr; التمهيد (skills والاستشهادات) |
| **Dev** | دورة تجا��ب تكرارية: تخطيط &rarr; تشغيل Slurm &rarr; تحل��ل &rarr; كتابة مسودة أولية |
| **Review** | تجميع &rarr; مراجعة &rarr; تخطيط &rarr; تنفيذ &rarr; تحقّق، تكرار حتى الدرجة &ge; العتبة |

### دورة المراجعة

كل تكرار في مرحلة المراجعة يمرّ بـ **٥ خطوات**:

<p align="center">
  <img src="https://idea2paper.org/assets/review_loop.png" alt="دورة المراجعة" width="700">
</p>

| الخطوة | الوصف |
|:-------|:------|
| **Compile** | LaTeX &rarr; PDF، عدّ الصفحات، استخراج صور الصفحات |
| **Review** | تقييم الورقة ١&ndash;١٠، سرد المشكلات الكبرى والصغرى |
| **Plan** | المخطط يُنشئ خطة عمل ذات أولويات |
| **Execute** | الباحث + المُجرِّب يعملان بالتوازي؛ الكاتب يعدّل LaTeX |
| **Validate** | التحقق من أن التغييرات تُترجَم بنجاح؛ إعادة إنتاج PDF |

تتكرر الحلقة حتى يصل التقييم إلى عتبة القبول — أو تتدخل عبر Telegram.

---

## ARK Agents

<p align="center">
  <img src="https://idea2paper.org/assets/architecture_overview.png" alt="بنية ARK" width="600">
</p>

| الوكيل | الدور |
|:-------|:------|
| **Researcher** | يحلّل المقترح &rarr; يكتب `idea.md`؛ مسح أدبي بدعم Gemini؛ يُخصّص قوالب الوكلاء للمشروع |
| **Reviewer** | يُقيّم الورقة وفق معايير المؤتمر ويُنشئ مهام تحسين؛ يتحقق من توافق التجارب مع المقترح |
| **Planner** | يحوّل ملاحظات المراجعة إلى خطة عمل ذات أولويات؛ يتحقق من توافق التجارب |
| **Writer** | يصيغ أقسام LaTeX ويُنقّحها بمراجع مُتحقَّقة عبر DBLP |
| **Experimenter** | يصمم التجارب، يُرسل مهام Slurm، ويحلل النتائج؛ دعم متعدد المزودين |
| **Coder** | يكتب ويُصحّح كود التجارب وسكريبتات التحليل |

---

## ما يميّز ARK

| | الأدوات الأخرى | ARK |
|---|:---------------|:----|
| **التحكم** | تعمل بلا إشراف، تنحرف عن النية، لا تصحيح أثناء التشغيل | إنسان في الحلقة: توقف عند القرارات الحرجة، تدخّل عبر Telegram أو الويب |
| **التنسيق** | تخطيطات مضطربة، أخطاء LaTeX، إصلاح يدوي | LaTeX مُبرمَج + قوالب مؤتمرات (NeurIPS، ACL، IEEE&hellip;) |
| **الاستشهادات** | LLM يختلق مراجع تبدو معقولة لكنها غير موجودة | كل اقتباس يُتحقَّق عبر DBLP — لا مراجع وهمية |
| **الأشكال** | أنماط افتراضية، أحجام خاطئة، لا مراعاة لقيود الصفحة | Nano Banana + أبعاد لوحة وعرض أعمدة وخطوط دقيقة |
| **العزل** | بيئة مشتركة — المشاريع تتداخل مع بعضها | بيئة conda لكل مشروع، HOME معزول، عزل كامل متعدد المستأجرين |
| **النزاهة** | LLM يحاكي النتائج بدلاً من تشغيل تجارب حقيقية | موجّهات مضادة للمحاكاة + مهارات مدمجة تفرض التنفيذ الحقيقي |

---

## عزل البيئات

يعمل كل مشروع في **بيئة conda مستقلة لكل مشروع**، مُستنسخة من بيئة أساسية عند الإنشاء. هذا يضمن عزلاً كاملاً متعدد المستأجرين:

- **Python معزول** &mdash; مجلد `.env/` لكل مشروع بحزمه الخاصة
- **HOME معزول** &mdash; كل مُنسق يعمل بمجلد المشروع كـ HOME
- **لا تلوث متبادل** &mdash; `PYTHONNOUSERSITE=1` يمنع تسرب حزم المستخدم
- **توفير تلقائي** &mdash; `ark run` وبوابة الويب يكتشفان ويستخدمان بيئة conda للمشروع؛ خط الأنابيب يُنشئها تلقائياً عند الحاجة

```bash
# بيئة conda تُنشأ تلقائياً عند أول تشغيل
# ark run يكتشفها ويستخدمها:
ark run myproject
#   Conda env: /path/to/projects/myproject/.env
```

## نظام المهارات

يأتي ARK مع **مهارات مدمجة** &mdash; مجموعات تعليمات نمطية يحمّلها الوكلاء أثناء التشغيل لفرض أفضل الممارسات:

| المهارة | الغرض |
|:--------|:-------|
| **research-integrity** | موجّهات مضادة للمحاكاة: الوكلاء يجب أن يُجروا تجارب حقيقية لا أن يختلقوا نتائج |
| **human-intervention** | بروتوكول التصعيد: الوكلاء يتوقفون ويسألون عبر Telegram قبل الإجراءات غير القابلة للعكس |
| **env-isolation** | فرض حدود البيئة لكل مشروع |
| **figure-integrity** | التحقق من تطابق محتوى الأشكال مع البيانات؛ منع الرسوم البيانية الوهمية |
| **page-adjustment** | الحفاظ على حدود الصفحات بتعديل كثافة المحتوى لا بحذف الأقسام |

المهارات موجودة في `skills/builtin/` وتُثبّت تلقائياً أثناء مرحلة التمهيد.

---

## البداية السريعة

```bash
# التثبيت
pip install -e .

# إنشاء مشروع (معالج تفاعلي)
ark new mma

# التشغيل — ARK يتولى من هنا
ark run mma

# المراقبة المباشرة
ark monitor mma

# التحقق من التقدم
ark status mma
```

يرشدك المعالج عبر: مجلد الشيفرة، المؤتمر المستهدف، فكرة البحث، المؤلفين، بيئة الحوسبة، توليد الرسوم، وإعداد Telegram.

### البدء من ملف PDF موجود

```bash
ark new mma --from-pdf proposal.pdf
```

يحلل ARK ملف PDF باستخدام PyMuPDF + Claude Haiku، ويملأ المعالج مسبقًا، ويبدأ من المواصفات المستخرجة.

---

## مرجع الأوامر

| الأمر | الوظيفة |
|:------|:--------|
| `ark new <name>` | إنشاء مشروع عبر معالج تفاعلي |
| `ark run <name>` | بدء الـ pipeline (يكتشف تلقائياً بيئة conda للمشروع) |
| `ark status [name]` | التقييم، التكرار، المرحلة، التكلفة |
| `ark monitor <name>` | لوحة مراقبة مباشرة: نشاط الوكلاء، اتجاه التقييم |
| `ark update <name>` | إدخال تعليمات أثناء التشغيل |
| `ark stop <name>` | إيقاف سلس |
| `ark restart <name>` | إيقاف وإعادة تشغيل |
| `ark research <name>` | تشغيل Gemini Deep Research بشكل مستقل |
| `ark config <name> [key] [val]` | عرض أو تعديل الإعدادات |
| `ark clear <name>` | إعادة تعيين الحالة للبدء من جديد |
| `ark delete <name>` | حذف المشروع بالكامل |
| `ark setup-bot` | إعداد بوت Telegram |
| `ark list` | سرد جميع المشاريع وحالتها |
| `ark webapp install` | تثبيت خدمة بوابة الويب |
| `ark access list` | عرض قائمة السماح في Cloudflare Access الخاصة بلوحة التحكم |
| `ark access add <email>` | إضافة بريد إلكتروني (أو عدة) إلى قائمة السماح في CF Access |
| `ark access remove <email>` | إزالة بريد إلكتروني (أو عدة) من قائمة السماح في CF Access |
| `ark access add-domain <domain>` | إضافة قاعدة نطاق بريد إلى CF Access |
| `ark access remove-domain <domain>` | إزالة قاعدة نطاق بريد من CF Access |

---

## بوابة الويب

يتضمن ARK بوابة ويب لإدارة المشاريع وعرض الدرجات وتوجيه الوكلاء. تعرض البوابة **شارات مراحل مباشرة** (Research / Dev / Review)، حالة بيئة conda لكل مشروع، وتتبع التكلفة في الوقت الحقيقي.

### الإعدادات

يتم تكوين تطبيق الويب عبر `webapp.env` الموجود في دليل إعدادات ARK (الافتراضي: `.ark/webapp.env` في جذر المشروع). يتم إنشاؤه تلقائيًا عند أول تشغيل لـ `ark webapp`.

#### المصادقة والوصول
- **SMTP**: مطلوب لتسجيل الدخول بالرابط السحري. عيّن `SMTP_HOST` و`SMTP_USER` و`SMTP_PASSWORD`.
- **تقييد الوصول**: استخدم `ALLOWED_EMAILS` (مستخدمون محددون) أو `EMAIL_DOMAINS` (مؤسسات كاملة).
- **Google OAuth**: اختياري. عيّن `GOOGLE_CLIENT_ID` و`GOOGLE_CLIENT_SECRET`.

### أوامر الإدارة

| الأمر | الوصف |
|:------|:------|
| `ark webapp` | بدء التطبيق في الواجهة (مفيد للتصحيح). |
| `ark webapp release` | وسم الكود الحالي ونشره في شجرة العمل الإنتاجية. |
| `ark webapp install [--dev]` | تثبيت وبدء كخدمة `systemd` للمستخدم. |
| `ark webapp status` | عرض حالة خدمة systemd. |
| `ark webapp restart` | إعادة تشغيل خدمة التطبيق. |
| `ark webapp logs [-f]` | عرض أو متابعة سجلات الخدمة. |

<details>
<summary><strong>تفاصيل الخدمة (إنتاج مقابل تطوير)</strong></summary>

| | الإنتاج | التطوير |
|--|:--------|:--------|
| **المنفذ** | 9527 | 1027 |
| **اسم الخدمة** | `ark-webapp` | `ark-webapp-dev` |
| **بيئة Conda** | `ark-prod` | `ark-dev` |
| **مصدر الكود** | `~/.ark/prod/` (مقفل) | المستودع الحالي (مباشر) |

</details>

<details>
<summary><strong>استدعاء المُنسق مباشرة</strong></summary>

```bash
python -m ark.orchestrator --project mma --mode paper --max-iterations 20
python -m ark.orchestrator --project mma --mode dev
```

</details>

---

## استخدام Docker

### متطلبات البنية المعمارية

> [!IMPORTANT]
> يعتمد نظام ARK للبحث على مكتبات علمية أكثر استقرارًا على معمارية x86_64. إذا كنت تبني على جهاز Mac بمعالج **Apple Silicon (M1/M2/M3)**، يجب أن تبني للمنصة `linux/amd64`.
>
> جميع ملفات Dockerfiles الخاصة بـ ARK وملف `docker-compose.yml` مُهيأة لإجبار `linux/amd64` بشكل افتراضي.

### التشغيل باستخدام Docker Compose

أسهل طريقة لتشغيل بوابة ويب ARK هي استخدام `docker-compose`. من جذر المشروع:

```bash
# تشغيل بوابة الويب (تبني الصورة تلقائيًا لـ amd64)
docker compose -f docker/docker-compose.yml up --build -d
```

ستكون بوابة الويب متاحة على `http://localhost:9527`. يتم حفظ جميع قواعد البيانات والإعدادات وبيانات المشاريع تلقائيًا في وحدة تخزين Docker المسماة (`ark_data`).

لعرض السجلات المباشرة لبوابة الويب:
```bash
docker compose -f docker/docker-compose.yml logs -f webapp
```

### الإعداد والتكوين

لتخصيص إعدادات بوابة الويب (مثل إعداد SMTP لتسجيل الدخول عبر الرابط السحري أو OAuth):

```bash
# إنشاء ملف إعداد مخصص
cp .ark/webapp.env.example .ark/webapp.env
# عدّل .ark/webapp.env ببياناتك الاعتمادية
```
ثم أزل التعليق عن تعيين وحدة تخزين بيئة البيئة في `docker/docker-compose.yml` تحت خدمة `webapp`:
```yaml
      - ../.ark/webapp.env:/data/.ark/webapp.env:ro
```

### تشغيل مهام بحثية مستقلة

يمكنك تشغيل مهام بحثية معزولة بجانب تطبيق الويب باستخدام حاوية مهام ARK. أزل التعليق عن خدمة `job` في `docker/docker-compose.yml`، ثم شغّل:

```bash
docker compose -f docker/docker-compose.yml run --rm job \
  --project myproject \
  --project-dir /data/projects/<user-id>/myproject \
  --mode research \
  --iterations 10
```

*ملاحظة: يجب تمرير مفاتيح API المطلوبة (مثل `ANTHROPIC_API_KEY` و`GEMINI_API_KEY`) كمتغيرات بيئة.*

### تشغيل الحاويات بشكل مستقل (مباشرة)

إذا كنت تفضل تشغيل الحاويات يدويًا دون Docker Compose:

#### 1. بناء الصور (إجبار amd64)
```bash
# بناء بوابة الويب
docker build --platform linux/amd64 -f docker/Dockerfile.webapp -t ark-webapp .

# بناء حاوية المهام
docker build --platform linux/amd64 -f docker/Dockerfile.job -t ark-job .
```

#### 2. تشغيل بوابة الويب
```bash
docker run -d --name ark-webapp \
  --platform linux/amd64 \
  -p 9527:9527 \
  -v ark_data:/data \
  ark-webapp
```

#### 3. تشغيل مهمة بحثية
```bash
docker run --rm -it \
  --platform linux/amd64 \
  -v ark_data:/data \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  ark-job \
  --project myproject \
  --project-dir /data/projects/myproject \
  --mode research
```

### الرفع إلى Google Cloud Platform (GCP)

يتضمن ARK سكريبت لبناء الصور ورفعها إلى Google Artifact Registry أو GCR.

```bash
# الرفع إلى Artifact Registry (موصى به)
./docker/push-gcp.sh --project [PROJECT_ID] --region [REGION] --repo [REPO] --build

# الرفع إلى Container Registry القديم (gcr.io)
./docker/push-gcp.sh --project [PROJECT_ID] --legacy --build
```
يقوم الخيار `--build` تلقائيًا ببناء الصور لـ `linux/amd64` حتى عند التشغيل على macOS.

---

## الحوسبة السحابية

يدعم ARK تشغيل التجارب على أجهزة افتراضية سحابية بعيدة (AWS أو GCP أو Azure) مع إبقاء المُنسِّق وبوابة الويب تعملان **محليًا**. هذا هو الإعداد الموصى به إذا كنت تريد قدرة حوسبة مرنة دون إدارة كتلة HPC.

**آلية العمل:**
1. يعمل تطبيق الويب محليًا أو على خادم صغير، ويتولى إدارة المشاريع والواجهة.
2. عند إرسال مشروع، يُهيّئ ARK جهازًا افتراضيًا سحابيًا، وينقل كود المشروع عبر SSH، ويدير دورة حياة التجربة الكاملة عن بُعد.
3. يتم مزامنة النتائج تلقائيًا. يتم إنهاء الجهاز الافتراضي عند اكتمال التشغيل.

### تفعيل الحوسبة السحابية عبر لوحة التحكم

1. افتح لوحة **الإعدادات** (أيقونة ⚙️ في شريط التنقل العلوي).
2. انتقل للأسفل إلى قسم **Cloud Compute**.
3. أدخل بياناتك الاعتمادية للمزوّد المفضل (AWS أو GCP أو Azure).
4. انقر **حفظ**. ستُرسَل جميع مشاريع اللاحقة تلقائيًا إلى السحابة.

> [!TIP]
> يتم تشفير بيانات الاعتماد السحابية في حالة السكون باستخدام `SECRET_KEY`. لا يتم تسجيل مفاتيحك أو إرسالها إلى جهات خارجية.

---

### إنشاء مشروع

بعد تهيئة الحوسبة السحابية، الطريقة الموصى بها لإطلاق مشروع هي عبر لوحة التحكم:

1. انقر **New Project** من الصفحة الرئيسية للوحة التحكم.
2. أدخل هدف البحث والمؤتمر المستهدف وأي تعليمات إضافية.
3. انقر **Submit** — يقوم تطبيق الويب تلقائيًا بإنشاء `config.yaml` للمشروع وتهيئة الجهاز الافتراضي السحابي.

يُحفظ ملف `config.yaml` المُولَّد في:

```
~/.ark/data/projects/<user_id>/<project_id>/config.yaml
```

يمكنك فحص هذا الملف أو تعديله يدويًا في أي وقت (مثلًا لضبط نوع النسخة أو إضافة `setup_commands`). تسري التغييرات في التشغيل أو إعادة التشغيل التالية.

> [!NOTE]
> إذا كان `PROJECTS_ROOT` مُعيَّنًا في `.ark/webapp.env`، يُستبدل المسار أعلاه بـ `$PROJECTS_ROOT/<user_id>/<project_id>/config.yaml`.

---

### إعداد مزودي السحابة

<details>
<summary><strong>☁️ Google Cloud Platform (GCP)</strong></summary>

#### 1. إنشاء حساب خدمة

```bash
export PROJECT_ID=your-gcp-project-id

# إنشاء حساب خدمة لـ ARK
gcloud iam service-accounts create ark-runner \
  --display-name="ARK Research Runner"

# منح الأدوار المطلوبة (Compute Admin + Service Account User)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ark-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/compute.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ark-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# تنزيل مفتاح JSON
gcloud iam service-accounts keys create ~/ark-gcp-key.json \
  --iam-account=ark-runner@${PROJECT_ID}.iam.gserviceaccount.com
```

#### 2. تفعيل واجهات برمجة التطبيقات المطلوبة

```bash
gcloud services enable compute.googleapis.com --project=$PROJECT_ID
```

#### 3. التكوين في لوحة التحكم

الصق محتوى `~/ark-gcp-key.json` في حقل **GCP Service Account JSON** وعيّن **GCP Project ID** في لوحة الإعدادات.

#### 4. مرجع `config.yaml` (متقدم / CLI فقط)

يُولّد تطبيق الويب هذا تلقائيًا من الإعدادات. للمشاريع اليدوية أو المُدارة عبر CLI، أضف ما يلي إلى `config.yaml` الخاص بمشروعك:

```yaml
compute_backend:
  type: cloud
  provider: gcp
  region: us-central1-a          # المنطقة (zone)، ليس الإقليم (region)
  instance_type: n1-standard-8
  image_id: common-cu121          # عائلة صور Deep Learning VM
  ssh_key_path: ~/.ssh/id_rsa
  ssh_user: user
  # اختياري: معجّل GPU
  accelerator_type: nvidia-tesla-t4
  accelerator_count: 1
  # اختياري: أوامر تُنفَّذ على النسخة بعد الإقلاع
  setup_commands:
    - conda activate base && pip install -r requirements.txt
```

</details>

---

<details>
<summary><strong>☁️ Amazon Web Services (AWS)</strong></summary>

#### 1. إنشاء مستخدم IAM

```bash
# إنشاء مستخدم IAM لـ ARK
aws iam create-user --user-name ark-runner

# إرفاق السياسة (EC2 full access كافٍ)
aws iam attach-user-policy \
  --user-name ark-runner \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess

# إنشاء مفاتيح الوصول
aws iam create-access-key --user-name ark-runner
# لاحظ AccessKeyId و SecretAccessKey من المخرجات
```

#### 2. إنشاء زوج مفاتيح SSH

```bash
# إنشاء زوج مفاتيح وحفظه محليًا
aws ec2 create-key-pair \
  --key-name ark-key \
  --query 'KeyMaterial' \
  --output text > ~/.ssh/ark-key.pem
chmod 600 ~/.ssh/ark-key.pem
```

#### 3. التكوين في لوحة التحكم

أدخل **AWS Access Key ID** و**AWS Secret Access Key** و**AWS Region** (مثل `us-east-1`) في لوحة الإعدادات.

#### 4. مرجع `config.yaml` (متقدم / CLI فقط)

يُولّد تطبيق الويب هذا تلقائيًا من الإعدادات. للمشاريع اليدوية أو المُدارة عبر CLI، أضف ما يلي إلى `config.yaml` الخاص بمشروعك:

```yaml
compute_backend:
  type: cloud
  provider: aws
  region: us-east-1
  instance_type: g4dn.xlarge        # 1x T4 GPU، 4 vCPUs، 16 GB RAM
  image_id: ami-0c7c51e8edb7b66d3   # Deep Learning AMI (Ubuntu 22.04)
  ssh_key_name: ark-key              # اسم زوج المفاتيح في AWS Console
  ssh_key_path: ~/.ssh/ark-key.pem
  ssh_user: ubuntu
  security_group: sg-xxxxxxxx        # يجب السماح بـ SSH الوارد (المنفذ 22)
  # اختياري: إعداد ما بعد الإقلاع
  setup_commands:
    - conda activate pytorch && pip install -r requirements.txt
```

> [!IMPORTANT]
> تأكد من أن مجموعة الأمان تسمح بـ **SSH الوارد (المنفذ 22)** من عنوان IP الجهاز الذي يُشغّل تطبيق الويب. بدون ذلك، لن يتمكن ARK من الاتصال بالنسخة المُهيَّأة.

</details>

---

<details>
<summary><strong>☁️ Microsoft Azure</strong></summary>

#### 1. إنشاء Service Principal

```bash
# تسجيل الدخول
az login

# إنشاء service principal بدور Contributor
az ad sp create-for-rbac \
  --name "ark-runner" \
  --role Contributor \
  --scopes /subscriptions/YOUR_SUBSCRIPTION_ID
# لاحظ: appId (Client ID) و password (Client Secret) و tenant (Tenant ID)
```

#### 2. تسجيل مفتاح SSH العام

```bash
# إنشاء مفتاح SSH إذا لم يكن لديك واحد
ssh-keygen -t rsa -b 4096 -f ~/.ssh/ark-azure-key

# سيُستخدم المفتاح العام (~/.ssh/ark-azure-key.pub) تلقائيًا
```

#### 3. التكوين في لوحة التحكم

أدخل **Azure Client ID** و**Azure Client Secret** و**Azure Tenant ID** و**Azure Subscription ID** في لوحة الإعدادات.

#### 4. مرجع `config.yaml` (متقدم / CLI فقط)

يُولّد تطبيق الويب هذا تلقائيًا من الإعدادات. للمشاريع اليدوية أو المُدارة عبر CLI، أضف ما يلي إلى `config.yaml` الخاص بمشروعك:

```yaml
compute_backend:
  type: cloud
  provider: azure
  region: eastus                     # موقع Azure
  instance_type: Standard_NC6s_v3    # 1x V100 GPU، 6 vCPUs، 112 GB RAM
  image_id: UbuntuLTS                # اسم مستعار لصورة نظام التشغيل
  ssh_key_path: ~/.ssh/ark-azure-key
  ssh_user: azureuser
  resource_group: ark-resources      # سيُنشأ إذا لم يكن موجودًا
  # اختياري: إعداد ما بعد الإقلاع
  setup_commands:
    - pip install -r requirements.txt
```

</details>

---

### ضبط التكاليف

> [!WARNING]
> تُفوتَر الأجهزة الافتراضية السحابية بالساعة. يُنهي ARK النسخ تلقائيًا بعد اكتمال كل تشغيل. ومع ذلك، إذا أُنهيت عملية تطبيق الويب بشكل غير متوقع، فإن آلية **Orphan Rescue** ستكتشف النسخ المتوقفة عند إعادة التشغيل التالية وتُعلّمها كفاشلة — لكنها **لن تُنهي الجهاز الافتراضي السحابي تلقائيًا**. تحقق دائمًا من عدم وجود نسخ معلّقة في لوحة تحكم سحابتك بعد عمليات الإيقاف غير المتوقعة.

---

## تكامل Telegram

```bash
ark setup-bot    # مرة واحدة: الصق رمز BotFather، كشف تلقائي لمعرّف المحادثة
```

ما تحصل عليه:
- **إشعارات غنية** &mdash; تغيرات التقييم المنسقة، انتقالات المراحل، نشاط الوكلاء، والأخطاء
- **إرسال تعليمات** &mdash; توجيه التكرار الحالي في الوقت الحقيقي
- **طلب PDF** &mdash; أحدث ورقة مترجمة تُرسل للمحادثة
- **تدخل بشري** &mdash; الوكلاء يصعّدون القرارات إليك قبل الإجراءات غير القابلة للعكس
- **متوافق مع HPC** &mdash; يدعم شهادات SSL الموقّعة ذاتياً على شبكات المؤسسات/HPC

---

## المتطلبات

- **Python 3.9+** مع `pyyaml` و `PyMuPDF`
- **CLI الوكيل**: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (موصى به، يتطلب اشتراك Claude Max) **أو** [Gemini CLI](https://github.com/google-gemini/gemini-cli) — قابل للاختيار لكل مشروع على حدة
- اختياري: LaTeX (`pdflatex` + `bibtex`)، Slurm، `google-genai` للرسوم الذكية

```bash
# إنشاء بيئة conda الأساسية
conda env create -f environment.yml         # Linux (ينشئ "ark-base")
# أو لنظام macOS:
conda env create -f environment-macos.yml   # macOS (ينشئ "ark-base")

pip install -e .                    # الأساسي
pip install -e ".[research]"       # + Gemini Deep Research و Nano Banana
```

## المؤتمرات المدعومة

NeurIPS &bull; ICML &bull; ICLR &bull; AAAI &bull; ACL &bull; IEEE &bull; ACM SIGPLAN &bull; ACM SIGCONF &bull; LNCS &bull; MLSys &bull; USENIX &mdash; بالإضافة إلى أسماء بديلة لـ PLDI، ASPLOS، SOSP، EuroSys، OSDI، NSDI، INFOCOM، وغيرها.

## الرخصة

[Apache 2.0](LICENSE)
