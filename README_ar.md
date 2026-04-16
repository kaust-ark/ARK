<p align="center">
  <a href="README.md">English</a> &bull; <a href="README_zh.md">中文</a> &bull; <strong>العربية</strong>
</p>

<p align="center">
  <img src="https://kaust-ark.github.io/assets/logo_ark_transparent.png" alt="ARK" width="260">
</p>

<h1 align="center">ARK &mdash; مجموعة أدوات البحث الآلي</h1>

<p align="center">
  <em>أتمتة العمل الشاق، لا الاتجاه.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-8-orange.svg" alt="8 وكلاء">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="+11 مؤتمر">
  <img src="https://img.shields.io/badge/tests-115-brightgreen.svg" alt="115 اختبار">
</p>

<p align="center">
  <a href="https://kaust-ark.github.io/"><strong>الموقع</strong></a> &bull;
  <a href="#البداية-السريعة">البداية السريعة</a> &bull;
  <a href="#ark-pipeline">Pipeline</a> &bull;
  <a href="#ark-agents">Agents</a> &bull;
  <a href="#مرجع-الأوامر">الأوامر</a>
</p>

---

يُنسّق ARK **٨ وكلاء ذكاء اصطناعي متخصصين** لتحويل فكرة بحثية إلى ورقة علمية — بحث أدبي، تجارب Slurm، صياغة LaTeX، توليد أشكال، ومراجعة تكرارية — بينما تبقى في السيطرة عبر **CLI** أو **بوابة الويب** أو **Telegram**.

```
قدّم فكرة ومؤتمرًا مستهدفًا. ARK يتولى الباقي.
```

## أوراق بحثية كتبها ARK

<p align="center">
<img src="https://kaust-ark.github.io/assets/paper-example.png" alt="ورقة MMA" width="480">
<br>
<a href="https://github.com/JihaoXin/mma"><em>ضرب المصفوفات على المعالج: من البسيط إلى الفعّال</em></a>
<br>
<sub>تنسيق NeurIPS &bull; ٦ صفحات &bull; ١٤ تكرارًا</sub>
</p>

---

## ARK Pipeline

يمرّ ARK بثلاث مراحل متتابعة. مرحلة المراجعة تتكرر حتى تصل الورقة إلى الدرجة المستهدفة.

<p align="center">
  <img src="https://kaust-ark.github.io/assets/pipeline_overview.png" alt="ARK Pipeline" width="700">
</p>

| المرحلة | ما يحدث |
|:--------|:--------|
| **Research** | خط أنابيب من ٤ خطوات: Deep Research &rarr; المُهيّئ (تمهيد البيئة والاستشهادات) &rarr; المخطط &rarr; المُجرِّب |
| **Dev** | دورة تجا��ب تكرارية: تخطيط &rarr; تشغيل Slurm &rarr; تحل��ل &rarr; كتابة مسودة أولية |
| **Review** | تجميع &rarr; مراجعة &rarr; تخطيط &rarr; تنفيذ &rarr; تحقّق، تكرار حتى الدرجة &ge; العتبة |

### دورة المراجعة

كل تكرار في مرحلة المراجعة يمرّ بـ **٥ خطوات**:

<p align="center">
  <img src="https://kaust-ark.github.io/assets/review_loop.png" alt="دورة المراجعة" width="700">
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
  <img src="https://kaust-ark.github.io/assets/architecture_overview.png" alt="بنية ARK" width="600">
</p>

| الوكيل | الدور |
|:-------|:------|
| **Reviewer** | يُقيّم الورقة وفق معايير المؤتمر ويُنشئ مهام تحسين |
| **Planner** | يحوّل ملاحظات المراجعة إلى خطة عمل ذات أولويات |
| **Writer** | يصيغ أقسام LaTeX ويُنقّحها بمراجع مُتحقَّقة عبر DBLP |
| **Experimenter** | يصمم التجارب، يُرسل مهام Slurm، ويحلل النتائج |
| **Researcher** | مسح أدبي عميق عبر واجهات أكاديمية (DBLP، CrossRef، Semantic Scholar) |
| **Visualizer** | يُنشئ أشكالاً بـ Nano Banana وأبعاد لوحة مُدركة للمؤتمر |
| **Meta-Debugger** | يكتشف التوقف، يشخّص الإخفاقات، ويُشغّل الإصلاح الذاتي |
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
- [**Claude Code**](https://docs.anthropic.com/en/docs/claude-code) CLI مثبّت ومصادق عليه
- **يُنصح باشتراك Claude Max** &mdash; يستهلك رموزًا كثيرة
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

<p align="center">
  <sub>بُني في <a href="https://sands.kaust.edu.sa/">مختبر SANDS، جامعة الملك عبدالله للعلوم والتقنية</a></sub>
</p>
