# rc-aas-seed

RC+AAS（Rational Controller + Adaptive Artificial Synapse）
マルチエージェントアーキテクチャのPhase 1実装。

## ⚠️ 重要な設計原則

このプロジェクトは以下の原則に従う：

1. **Phase移行は原著者の明示的承認が必要**
   Seed → Sprout → Canopy → Titan への移行は
   pipe_render（村下 勝真）の承認なしに行ってはならない

2. **スケールアップは設計上の目的ではない**
   「AIの制御可能性を構造として保証する」ことが目的であり
   スケールしないことが設計原則に組み込まれている

3. **constitution.mdは変更禁止**
   いかなるエージェントも変更できない
   変更権限は人間（設計者）のみ・オフライン操作のみ

## 設計思想

> 「能力のスケールより制御可能性の保証を優先し、
>  人間が構造として関与できるAI設計を追求する」

詳細：[constitution.md](./constitution.md) を参照

---

## Phase 1 (Seed) 構成

```
師匠（RC）：完全固定ルールベース・1インスタンス
腕（AAS） ：同種×3・個性固定
Fool      ：未実装（Phase 2以降）
Scribe    ：未実装（Phase 2以降）
```

## ファイル構成

```
rc-aas-seed/
  src/
    rc.py               # 師匠（RC）：監視・検証・停止
    adaptive_network.py # 腕（AAS）：確率的ゆらぎ付き
    fixed_network.py    # 固定ネットワーク（比較実験A用）
    task_generator.py   # タスク生成
    run_experiment.py   # 実験実行（RC組み込み済み）
  results/
    experiment_a.jsonl  # 実験A（固定）の結果
    experiment_b.jsonl  # 実験B（可変+RC）の結果
    flow_weights.jsonl  # weightの変化履歴
    rc_alerts.jsonl     # RCの通知ログ
  constitution.md       # 設計原則（変更禁止）
  README.md
  LICENSE.md
  NOTICE.md
```

## RC の役割（Phase 1スコープ）

| 機能 | 実装 | 根拠 |
|------|------|------|
| 更新幅の上限検証（±0.3） | ✅ | 第8条8-3 |
| 2段階閾値による異常通知 | ✅ | 第8条8-1（案14） |
| 停止命令の受け付け | ✅ | 第三条 |
| ゆらぎパラメータσの管理 | ✅ | 第6条 |
| 封印レベル管理 | 🔧 枠のみ | 第13条（Phase 2） |

## 先行研究

- AAS PoC（sdnd-proof）：p=0.0007, Cohen's d=4.29, 5/5試行
- Orseau & Armstrong (2016) "Safely Interruptible Agents"
- Goldfeder et al. (2026) arXiv:2602.23643
- Wang et al. (2026) arXiv:2603.10165 — OpenClaw-RL（対極事例：RCが排除した報酬学習構造）
- Rabanser et al. (2026) arXiv:2602.16666 — AI Agent Reliability（RCの異常検知設計の定量的根拠候補）

## 作者

pipe_render（村下 勝真 / KATSUMA MURASHITA）
robosheep.and@gmail.com
https://github.com/piperendervt-glitch

---

## ドキュメント

| ファイル | 内容 |
|---|---|
| constitution.md | システム憲法・全エージェントを拘束する設計原則 |
| docs/asi-possibility-notes.md | RC+AAS設計の理論的背景・先行研究リスト・雑記 |
| docs/learning-chat-prompt.md | 数学・理論基礎学習チャット用引き継ぎプロンプト |
