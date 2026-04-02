<p align="center">
  <a href="README.md">English</a> &bull; <a href="README_zh.md">中文</a> &bull; <strong>العربية</strong>
</p>

<p align="center">
  <img src="https://kaust-ark.github.io/assets/logo.png" alt="ARK" width="280">
</p>

<h1 align="center">ARK: مجموعة أدوات البحث الذكي</h1>

<p align="center">
  <strong>من فكرة بحثية إلى ورقة جاهزة للنشر — بشكل مستقل تمامًا</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-8-orange.svg" alt="8 agents">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="11+ venues">
</p>

<p align="center">
  <a href="https://kaust-ark.github.io/ARK/"><strong>الموقع</strong></a> &bull;
  <a href="#البداية-السريعة">البداية السريعة</a> &bull;
  <a href="#كيف-يعمل">كيف يعمل</a> &bull;
  <a href="#مرجع-الأوامر">الأوامر</a> &bull;
  <a href="https://kaust-ark.github.io/architecture.md">البنية</a> &bull;
  <a href="https://kaust-ark.github.io/configuration.md">الإعدادات</a>
</p>

---

يقوم ARK بتنسيق 8 وكلاء ذكاء اصطناعي متخصصين **لتخطيط التجارب، وكتابة الشيفرة، وتشغيل اختبارات الأداء، وصياغة أوراق LaTeX، ومراجعتها تكرارياً** عبر مراجعة أقران آلية — حتى تصل الورقة إلى جودة النشر.

قدّم فكرة بحثية ومؤتمرًا مستهدفًا. ARK يتولى الباقي.

## أوراق بحثية كتبها ARK

<p align="center">
<img src="https://kaust-ark.github.io/assets/paper-example.png" alt="ورقة MMA" width="480">
<br>
<a href="https://github.com/JihaoXin/mma"><em>ضرب المصفوفات على المعالج: من البسيط إلى الفعّال</em></a>
<br>
<sub>تنسيق NeurIPS &bull; ٦ صفحات &bull; ١٤ تكرارًا</sub>
</p>

## الميزات الرئيسية

| | الميزة | التفاصيل |
|---|-------|----------|
| **٨ وكلاء** | المراجع، المخطط، المُجرِّب، الكاتب، الباحث، المُصوِّر، مُصحح الأخطاء، المبرمج | مع أوامر مخصصة لكل مشروع |
| **٣ مراحل** | Research &rarr; Dev &rarr; Review | مسح أدبي، تجارب، تحسين الورقة |
| **Claude Code** | مبني على [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | يُنصح باشتراك Max — يستهلك رموزًا كثيرة |
| **+١١ مؤتمر** | NeurIPS، ICML، ICLR، AAAI، ACL، IEEE، ACM، LNCS... | ضبط تلقائي لهندسة الصفحة وحجم الرسوم |
| **بوت تيليغرام** | مراقبة وتدخل في الوقت الحقيقي | تأكيدات استباقية عند القرارات المهمة |
| **الحوسبة** | Slurm &bull; Local &bull; AWS &bull; GCP &bull; Azure | تشغيل التجارب في أي مكان |
| **البحث المعمّق** | تكامل Gemini Deep Research | مسح أدبي قبل بدء الكتابة |
| **Nano Banana** | توليد رسوم بالذكاء الاصطناعي | مخططات مفاهيمية عبر نماذج Gemini |
| **استرداد ذكي** | نقاط حفظ &bull; تصحيح تلقائي &bull; إصلاح ذاتي | معالجة أخطاء LaTeX وفشل التجارب |
| **تتبع التكلفة** | تقارير لكل تكرار وتراكمية | معرفة دقيقة بتكلفة كل تكرار |

## كيف يعمل

يمر ARK بثلاث مراحل متتابعة:

<p align="center">
  <img src="https://kaust-ark.github.io/assets/phases_overview.png" alt="مراحل ARK" width="800">
</p>

| المرحلة | ما يحدث |
|---------|---------|
| **البحث** | Gemini Deep Research يجمع المعرفة الخلفية والمسح الأدبي |
| **التطوير** | دورة تجارب تكرارية: تخطيط → تشغيل → تحليل → تقييم → كتابة مسودة أولية |
| **المراجعة** | حلقة تحسين الورقة حتى يصل تقييم المراجع إلى عتبة القبول |

### خطوات مرحلة المراجعة

كل تكرار في مرحلة المراجعة يمر بـ ٤ خطوات:

<p align="center">
  <img src="https://kaust-ark.github.io/assets/review_phase_steps.png" alt="خطوات مرحلة المراجعة" width="700">
</p>

| الخطوة | ما يحدث |
|--------|---------|
| **١. الترجمة** | LaTeX → PDF، عدّ الصفحات، استخراج صور الصفحات |
| **٢. المراجعة** | تقييم الورقة (١–١٠)، تحديد المشكلات الكبرى والصغرى |
| **٣. التخطيط والتنفيذ** | بناء خطة عمل؛ الباحث والمُجرِّب يعملان بالتوازي؛ الكاتب يعدّل LaTeX |
| **٤. التصوير** | فحص أبعاد الرسوم وفقًا لمواصفات المؤتمر، إصلاح تلقائي، إعادة ترجمة |

تتكرر الحلقة حتى يصل التقييم إلى عتبة القبول — أو تتدخل عبر تيليغرام.

### البنية

<p align="center">
  <img src="https://kaust-ark.github.io/assets/architecture.png" alt="بنية ARK" width="700">
</p>

<p align="center">
  <a href="https://kaust-ark.github.io/architecture.md">وثائق البنية الكاملة &rarr;</a>
</p>

## البداية السريعة

أولاً، أنشئ بيئة Python افتراضية وفعّلها باستخدام الأداة التي تفضلها (conda أو uv أو venv أو غيرها).

```bash
# ١. التثبيت
pip install -e .

# ٢. إنشاء مشروع (معالج تفاعلي)
ark new mma                    # مثال: ورقة عن ضرب المصفوفات

# ٣. التشغيل — ARK يتولى من هنا
ark run mma                    # يبدأ حلقة Research → Dev → Review

# ٤. المراقبة المباشرة
ark monitor mma                # لوحة مباشرة: الوكلاء، اتجاه التقييم

# ٥. التحقق من التقدم
ark status mma                 # التقييم: 7.2/10، التكرار: 5، المرحلة: Review
```

يرشدك المعالج عبر: مجلد الشيفرة، المؤتمر المستهدف، فكرة البحث، المؤلفين، بيئة الحوسبة، توليد الرسوم، وإعداد تيليغرام.

### البدء من ملف PDF موجود

```bash
# استخراج العنوان والمؤلفين وخطة البحث من مقترح/مسودة
ark new mma --from-pdf proposal.pdf
```

يحلل ARK ملف PDF باستخدام PyMuPDF + Claude Haiku، ويملأ المعالج مسبقًا، ويمكنه بدء مشروع ورقة أو تطوير كامل من المواصفات المستخرجة.

## مرجع الأوامر

| الأمر | الوظيفة |
|-------|---------|
| `ark new <name>` | إنشاء مشروع عبر معالج تفاعلي |
| `ark run <name>` | بدء الحلقة المستقلة |
| `ark status [name]` | التقييم، التكرار، المرحلة، التكلفة (أو سرد جميع المشاريع) |
| `ark monitor <name>` | لوحة مراقبة مباشرة: نشاط الوكلاء، اتجاه التقييم |
| `ark update <name>` | إدخال تعليمات أثناء التشغيل |
| `ark stop <name>` | إيقاف سلس |
| `ark restart <name>` | إيقاف وإعادة تشغيل |
| `ark research <name>` | تشغيل Gemini Deep Research بشكل مستقل |
| `ark config <name> [key] [val]` | عرض أو تعديل الإعدادات |
| `ark clear <name>` | إعادة تعيين الحالة للبدء من جديد |
| `ark delete <name>` | حذف المشروع بالكامل |
| `ark setup-bot` | إعداد بوت تيليغرام (مرة واحدة) |
| `ark list` | سرد جميع المشاريع وحالتها |
| `ark webapp install` | تثبيت خدمة بوابة الويب للإنتاج (المنفذ 8423) |
| `ark webapp install --dev` | تثبيت خدمة بوابة الويب للتطوير (المنفذ 8424) |
| `ark webapp uninstall` | إيقاف وإزالة خدمة الإنتاج |
| `ark webapp release` | وسم ونشر الكود الحالي إلى بيئة الإنتاج |

> **ملاحظة:** `ark webapp --daemon` مهمل وسيتم إزالته في إصدار مستقبلي. استخدم `ark webapp install` بدلاً منه.

<details>
<summary><strong>بوابة الويب (تطوير/إنتاج)</strong></summary>

يوفر ARK بيئتين ويب بقواعد بيانات منفصلة:

| | الإنتاج | التطوير |
|--|---------|---------|
| URL | `http://mcmgt01:8423` | `http://mcmgt01:8424` |
| الخدمة | `ark-webapp` | `ark-webapp-dev` |
| قاعدة البيانات | `ark_webapp/webapp.db` | `ark_webapp/webapp-dev.db` |
| الكود | `~/.ark/prod/` (مقفل على وسم git) | المستودع الحالي (مباشر) |

```bash
# النشر الأول
ark webapp release              # إنشاء بيئة الإنتاج من الكود الحالي (وسم v0.1.0)
ark webapp install              # بدء خدمة الإنتاج (المنفذ 8423)
ark webapp install --dev        # بدء خدمة التطوير (المنفذ 8424)

# نشر تغييرات جديدة للإنتاج
ark webapp release              # وسم، تحديث worktree الإنتاج، إعادة تشغيل الخدمة

# وسم إصدار مخصص
ark webapp release --tag v1.0.0
```

بيئة التطوير تعكس تغييرات الكود فوراً. بيئة الإنتاج تُحدّث فقط عند تنفيذ `ark webapp release`.

</details>

<details>
<summary><strong>استدعاء المُنسق مباشرة</strong></summary>

```bash
# وضع الورقة، بحد أقصى ٢٠ تكرارًا
python -m ark.orchestrator --project mma --mode paper --max-iterations 20

# وضع التطوير (تطوير برمجيات، ليس كتابة أوراق)
python -m ark.orchestrator --project mma --mode dev

# التشغيل في الخلفية
nohup python -m ark.orchestrator --project mma --mode paper \
  > auto_research/logs/orchestrator.log 2>&1 &
```

</details>

## تكامل تيليغرام

### خطوات الإعداد

1. افتح تيليغرام، أرسل `/newbot` إلى [@BotFather](https://t.me/BotFather) واتبع التعليمات للحصول على **Bot Token**
2. شغّل معالج الإعداد:
   ```bash
   ark setup-bot
   ```
3. الصق Bot Token عند الطلب
4. أرسل أي رسالة إلى بوتك الجديد في تيليغرام، ثم اضغط Enter
5. يكتشف ARK معرّف المحادثة تلقائيًا ويرسل رسالة اختبار

تُحفظ البيانات في `~/.ark/telegram.yaml` وتُشارَك بين جميع المشاريع.

### ما تحصل عليه

- **إشعارات مباشرة** — تغيرات التقييم، انتقالات المراحل، الأخطاء
- **إرسال تعليمات** — أرسل رسالة لتوجيه التكرار الحالي
- **طلب PDF** — احصل على أحدث ورقة مترجمة
- **تأكيدات استباقية** — يسأل ARK قبل بدء Deep Research أو عند الحاجة لرابط قالب LaTeX
- **عفريت مستمر** — يستمر في الاستجابة حتى عند توقف المُنسق

## المتطلبات

- **Python 3.9+** مع `pyyaml` و `PyMuPDF`
- [**Claude Code**](https://docs.anthropic.com/en/docs/claude-code) CLI مثبّت ومصادق عليه
- **يُنصح بشدة باشتراك Claude Max** — يستهلك ARK رموزًا كثيرة جدًا (كل تكرار يستدعي وكلاء متعددين)
- اختياري: LaTeX (`pdflatex` + `bibtex`)، Slurm، `google-genai` للرسوم الذكية

```bash
pip install -e .                    # الأساسي (يتضمن PyMuPDF)
pip install -e ".[research]"       # + Gemini Deep Research و Nano Banana
```

## المزيد

- [البنية ومرجع الوحدات](https://kaust-ark.github.io/architecture.md)
- [الإعدادات والمؤتمرات وبيئات الحوسبة](https://kaust-ark.github.io/configuration.md)
- [الاختبارات (٨٤ اختبارًا)](docs/testing.md)

## خارطة الطريق والمشكلات المعروفة

انظر [TODO.md](TODO.md) للقائمة الكاملة. أبرز النقاط:

- **تكامل المهارات التخصصية** — دمج [claude-scientific-skills](https://github.com/K-Dense-AI/claude-scientific-skills) (أكثر من ١٧٠ مهارة في البيولوجيا والكيمياء والفيزياء والجغرافيا والمالية وغيرها) لدعم الباحثين من خارج علوم الحاسوب
- **تكافؤ الواجهات الخلفية** — واجهتا Codex و Gemini لم تصلا بعد إلى التكافؤ الكامل مع Claude Code
- **التحقق من الحوسبة السحابية** — أكواد AWS/GCP/Azure موجودة لكن لم تُختبر من البداية للنهاية
- **البيئات المحدودة والمخصصة** — دعم HPC بدون إنترنت، Jetson، مختبرات ذات اتصال محدود
- **جودة تنسيق الرسوم** — تجاوز عرض العمود، عدم تطابق حجم الخط، مشاكل محاذاة الرسوم الفرعية
- **مصداقية الاستشهادات** — المراجع المولّدة بالذكاء الاصطناعي قد تكون وهمية؛ يلزم التحقق بعد الكتابة عبر Semantic Scholar / CrossRef
- **اختبار التكامل** — لا يوجد اختبار شامل للمسار بعد

## الرخصة

[Apache 2.0](LICENSE)
