# 分发说明（管理员）

> 给同事的操作指南是 ONBOARDING.md，这份不要一起发。

## 1. 生成共享配置

```bash
fecho export-shared-config
```

默认写到 `~/.fecho/fecho-团队LLM配置.json`，权限 600，只含 LLM 那几个字段
（Mobius token 是一人一份的身份凭证，不会被导出）。

它导出的就是这三个必需字段：

```json
{
  "llm_base_url": "http://api.feedmob.it.com",
  "llm_api_key": "……",
  "llm_model": "minimax-m3"
}
```

## 2. 怎么发给同事

**这个文件里有 API key，仓库又是公开的。**

- ✅ 私聊单独发，或者放内网共享盘
- ❌ 不要发群里、不要进 Git、不要传公开网盘

仓库的 `.gitignore` 已经挡了 `fecho-团队LLM配置.json` 和 `*shared-config*.json`，
但换个文件名还是会漏，注意别乱改名。

## 3. 千万别用 `pip install -e`

后台服务必须用**普通安装**（`pip install .`），不能用可编辑安装。

踩过的坑：可编辑安装靠一个 `.pth` 钩子定位代码，`import fecho` 能成功，但
`import fecho.cli` 在 launchd 那种干净环境里会失败 —— 两个后台服务会一直起不来，
日志里刷 `No module named fecho.cli`，而在终端里手动跑一切正常，非常难查。

改代码之后要让服务用上新代码：

```bash
~/.fecho/venv/bin/pip install ".[server]"
fecho schedule install --time 21:00
```

## 4. 收集反馈时重点问

- 归属准不准（**这是唯一没在第二个人身上验证过的部分**）
- 日报有没有漏掉重要的事
- 21:00 那个时间点合不合适
