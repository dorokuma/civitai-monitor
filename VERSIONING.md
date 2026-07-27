# 版本迭代规则 (Versioning Policy)

本仓库遵循 [SemVer 2.0.0](https://semver.org/lang/zh-CN/) 版本号格式：`MAJOR.MINOR.PATCH`。

## 版本来源

- `VERSION` 文件是版本号的**唯一来源**（single source of truth），所有版本号以该文件内容为准。
- 提交信息采用 [Conventional Commits](https://www.conventionalcommits.org/) 规范，前缀决定版本 bump 方式：

| 提交前缀 | 版本影响 |
| --- | --- |
| `fix:` | PATCH 递增 |
| `feat:` | MINOR 递增 |
| `perf:` / `refactor:` / `docs:` / `test:` / `chore:` | 不单独 bump，随下一次 release 合并计入 |
| 含 `BREAKING CHANGE` 或 scope 后带 `!:` | MAJOR 递增 |

- All commit messages and tag annotations MUST be written in English only.

- 破坏性变更（MAJOR）体现为：提交正文含 `BREAKING CHANGE:` 描述，或标题中 scope 之后使用 `!:`，例如 `feat(api)!: 移除旧接口`。

## 发布流程 (Release)

1. 在 `main` 分支上更新 `VERSION` 文件到目标版本号。
2. 更新 `CHANGELOG.md`，按 [Keep a Changelog](https://keepachangelog.com/) 格式新增对应版本条目。
3. 在 `main` 上打 **annotated tag**：`git tag -a vX.Y.Z -m "vX.Y.Z: <摘要>"`。
4. 推送分支与标签：`git push origin main && git push origin vX.Y.Z`。

## 预 1.0 阶段

- 在达到 `1.0.0` 之前（pre-1.0 阶段），`MINOR` 版本允许包含破坏性变更，无需 bump 到 `MAJOR`。
- 达到 `1.0.0` 之后，破坏性变更必须 bump `MAJOR`。
