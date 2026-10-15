# 【交付复核请求 / Review Request Package】

> **说明**：本模板为实施 Agent（如 Antigravity / Cursor / Aider）向审查 Agent（Claude Code / Codex）发起正式复核的标准化文件契约。

---

## 1. 目标与背景 (Context & Goal)
- **阶段里程碑**：Phase X / 功能名称
- **核心目标**：简要说明本阶段要解决的问题或交付的价值
- **关联设计 / Issue**：指向具体需求或架构文档路径

## 2. 变更范围与核心文件 (Scope & File Changes)
- `path/to/file1.ts`: 简要说明改动意图（如新增接口、优化缓存）
- `path/to/file2.py`: 简要说明改动意图（如修复越界、补充单元测试）

## 3. 本地自测与客观证据 (Self-Verification & Gate Results)
- **执行的测试命令**：
  ```bash
  pnpm gate
  # 或 pytest -v tests/
  ```
- **测试结果摘要**：
  - Pass: 42 / 42
  - Failed: 0
  - Coverage: 94.2%
- **客观输出证据（节选日志）**：
  ```text
  [PASS] tests/unit/auth.test.ts (1.2s)
  [PASS] tests/e2e/workflow.test.ts (3.4s)
  ```

## 4. 边界与未决事项 (Known Constraints & Open Questions)
- [ ] 针对高并发场景下的连接池未作压测，建议在下个阶段专项验证。
- [ ] 某外部依赖升级可能影响弃用 API，请复核是否存在兼容隐患。

## 5. 审查指令与期望输出 (Review Instructions for Auditor)
请独立复核上述改动，并按照如下标准给出结论：
1. **独立重跑验证**：请在你的环境中执行验证命令，确保证据真实可信。
2. **规范与边界审计**：审查是否有潜在的死锁、边界漏洞、类型安全违背或架构侵入。
3. **输出裁决清单**：
   - **通过 (Sign-off)**：无阻断问题，可以直接合入并推进下一阶段。
   - **有缺陷 (Action Required)**：列出具体的 `P0 (阻断)`、`P1 (严重)`、`P2 (建议优化)` 问题列表与整改建议。
