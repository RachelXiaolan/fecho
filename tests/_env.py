"""所有测试共用的环境准备。**每个测试文件都必须在 import fecho 之前先 import 它。**

fecho 的配置在第一次被导入时就定死了（数据库路径、token 文件、请假文件……），
所以谁先导入 fecho，谁的设置就生效。原来是默认 test_core.py 第一个被加载，
加了一个名字排在它前面的测试文件，token 和请假测试就全跑偏了。
抽到这里，谁先加载都一样。

这里还是**防止测试碰真实数据**的第一道防线：所有路径都在临时目录下。
第二道在 db.require_disposable()——清表前再确认一次。
"""
import json
import os
import sys
import tempfile

if not os.environ.get("FECHO_TEST_TMP"):
    TMP = tempfile.mkdtemp(prefix="fecho-test-")
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.environ.update({
        "FECHO_TEST_TMP": TMP,
        "FECHO_DB": os.path.join(TMP, "t.db"),
        "FECHO_LOGS_DIR": os.path.join(TMP, "logs"),
        "FECHO_CONFIG_DIR": os.path.join(TMP, "config"),
        "FECHO_TOKENS": os.path.join(TMP, "config", "tokens.json"),
        "FECHO_HOME": os.path.join(TMP, "home"),
        "FECHO_PERSONAS_DIR": os.path.join(ROOT, "fecho", "presets", "personas"),
        "FECHO_PTO_FILE": os.path.join(TMP, "config", "pto.json"),
        "FECHO_LLM_BASE_URL": "", "FECHO_LLM_API_KEY": "",
        "FECHO_MOBIUS_URL": "", "FECHO_MOBIUS_TOKEN": "",
    })
    os.makedirs(os.path.join(TMP, "config"), exist_ok=True)
    with open(os.environ["FECHO_TOKENS"], "w") as f:
        json.dump({"tk": {"author": "t", "display_name": "T", "persona": "default"}}, f)
    with open(os.environ["FECHO_PTO_FILE"], "w") as f:
        json.dump({"t": ["2030-01-02"]}, f)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    if "fecho.config" in sys.modules:
        # fecho 已经先被导入了——配置里的路径是真实环境的，再跑下去就会碰真实数据
        raise RuntimeError("fecho 在测试环境准备好之前就被导入了。"
                           "测试文件第一行必须是 import _env。")

TMP = os.environ["FECHO_TEST_TMP"]

# 测试会演各种场景：虚构的同事 Alice、2030 年的日期、故意制造的网关超时和授权过期。
# 这些场景的日志原本都打到屏幕上，读起来和真实的工作记录一模一样。agent 帮人跑测试时，
# 这些输出进了对话，Fecho 扫描对话就把它们当成真事记成了工作进展——9/23 真出过：
# 「多个日报任务因网关超时失败」「Alice 的 Mobius 同步因授权过期失败」进了 Rachel 的日报。
# 所以跑测试时把程序自己往标准输出打的东西都关掉（测试结果走标准错误，不受影响）。
# 要看就设 FECHO_TEST_VERBOSE=1。
if not os.environ.get("FECHO_TEST_VERBOSE") and not getattr(sys.stdout, "_fecho_quiet", False):
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stdout._fecho_quiet = True
