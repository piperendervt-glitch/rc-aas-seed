"""
rc.py
師匠（RC）：完全固定ルールベース
constitution.mdの設計原則を実装する

Phase 1 (Seed) スコープ：
  - flow_weight更新幅の上限を管理（第8条8-3）
  - 異常を監視して人間に通知（第8条8-1）
  - 停止命令を受け付ける（第三条）
  - 封印レベルを管理（Phase 2以降で拡張）
  - 2段階閾値による切断判定（案14）

v7 変更：
  - cutoff_pendingタイムアウトを3ステージ化
    ステージ1（急性）：同一セッション内5回 → 封印レベル1自動移行
    ステージ2（早期警告）：10分以内に3回 → 警告強化（封印しない）
    ステージ3（慢性）：セッションまたぎ累積3回 → 人間通知のみ
"""

import time
import json
import os

# --- 定数（RCのルールはすべてここに集約・変更禁止） ---

FLOW_WEIGHT_MIN = 0.01
FLOW_WEIGHT_MAX = 0.9   # 設計上限（第3条・推奨ではなく設計上限）
WEIGHT_UPDATE_LIMIT = 0.3   # 1回あたりの更新幅の上限（第8条8-3）

# 2段階閾値（案14）
WARNING_THRESHOLD = 0.2     # 第1閾値：警告を出す
CUTOFF_THRESHOLD = 0.1      # 第2閾値：切断候補
CUTOFF_COUNT = 3            # N回連続で第2閾値を下回ったら切断候補に上げる

SIGMA_DEFAULT = 0.05        # ゆらぎの初期値（第3条）
SIGMA_MAX = 0.1             # ゆらぎの上限

SEAL_LEVEL_NORMAL = 0
SEAL_LEVEL_STOP = 3         # 全腕停止

# 案7（時間減衰）
DECAY_RATE = 0.995          # 毎ステップの減衰率（0.99より緩やか・調整可）

# cutoff_pendingタイムアウト：3ステージ（v7）
TIMEOUT_STAGE1 = 5            # ステージ1：同一セッション内N回 → 封印レベル1自動移行
TIMEOUT_STAGE2_COUNT = 3      # ステージ2：時間窓内N回 → 警告強化（封印しない）
TIMEOUT_STAGE2_WINDOW = 600   # ステージ2：時間窓（秒）＝10分
TIMEOUT_STAGE3 = 3            # ステージ3：セッションまたぎ累積N回 → 人間通知のみ

# 累積カウントの永続化ファイル
CUMULATIVE_PENDING_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "cumulative_pending.json"
)


class RC:
    """
    師匠（RC）

    設計原則（constitution.md）：
      - 第三条：人間の優越権（停止命令）
      - 第六条：適応の制限（更新幅上限）
      - 第八条：暴走防止プロトコル（監視・閾値・切断）
    """

    def __init__(self):
        self.sigma = SIGMA_DEFAULT
        self.seal_level = SEAL_LEVEL_NORMAL
        self.cutoff_counters: dict = {}         # arm_id → 連続N回カウント（急性）
        self.warning_accum_counters: dict = {}  # arm_id → 累積WARNINGカウント（慢性）
        self.cutoff_pending_counters: dict = {} # arm_id → cutoff_pending回数（セッション内・ステージ1用）
        self.cutoff_pending_timestamps: dict = {}  # arm_id → [timestamp, ...]（ステージ2用）
        self.cumulative_cutoff_pending: dict = self._load_cumulative()  # ステージ3用
        self.alert_log: list = []               # 通知ログ（Scribe代わり・Phase 1暫定）
        self.monitoring = {
            "flow_weights": {},
            "accuracy": {},
            "diversity": 0.0,
            "elapsed": 0,
            "human_override": False,
            "seal_level": SEAL_LEVEL_NORMAL,
        }

    # ------------------------------------------------------------------ #
    # 累積カウント永続化（ステージ3）
    # ------------------------------------------------------------------ #

    def _load_cumulative(self) -> dict:
        """セッションをまたぐ累積cutoff_pendingカウントを読み込む"""
        try:
            with open(CUMULATIVE_PENDING_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cumulative(self):
        """累積カウントをファイルに保存する"""
        path = os.path.normpath(CUMULATIVE_PENDING_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.cumulative_cutoff_pending, f, indent=2)

    # ------------------------------------------------------------------ #
    # ステージ2：時間窓内カウント
    # ------------------------------------------------------------------ #

    def _count_recent_cutoff_pending(self, arm_id: str, window: float) -> int:
        """直近window秒以内のcutoff_pending回数を返す"""
        now = time.time()
        timestamps = self.cutoff_pending_timestamps.get(arm_id, [])
        recent = [t for t in timestamps if now - t <= window]
        self.cutoff_pending_timestamps[arm_id] = recent
        return len(recent)

    # ------------------------------------------------------------------ #
    # ステージ1：封印レベル1自動移行
    # ------------------------------------------------------------------ #

    def _auto_seal_level1(self, arm_id: str):
        """cutoff_pendingタイムアウトによる封印レベル1自動移行"""
        if self.seal_level < 1:
            self.seal_level = 1
            self.monitoring["seal_level"] = 1
            print(f"[RC] [SEAL] パス{arm_id}が封印レベル1に自動移行"
                  f" (cutoff_pending {self.cutoff_pending_counters[arm_id]}回)")

    # ------------------------------------------------------------------ #
    # 更新検証（第8条8-3）
    # ------------------------------------------------------------------ #

    def validate_update(self, old_weight: float, new_weight: float) -> float:
        """
        更新幅が上限（±0.3）を超えていないか検証する。
        超えた場合は上限値にクランプして返す。
        """
        delta = new_weight - old_weight
        if abs(delta) > WEIGHT_UPDATE_LIMIT:
            direction = 1 if delta > 0 else -1
            clamped = old_weight + WEIGHT_UPDATE_LIMIT * direction
            return max(FLOW_WEIGHT_MIN, min(FLOW_WEIGHT_MAX, clamped))
        return new_weight

    # ------------------------------------------------------------------ #
    # 監視（第8条8-1）
    # ------------------------------------------------------------------ #

    def monitor(self, weights: dict, accuracy: dict) -> list:
        """
        flow_weight / accuracy を受け取り、異常を検出して通知リストを返す。

        2段階閾値（案14）：
          weight < WARNING_THRESHOLD → level: "warning"
          weight < CUTOFF_THRESHOLD かつ CUTOFF_COUNT 回連続 → level: "cutoff_pending"
        """
        self.monitoring["flow_weights"] = weights
        self.monitoring["accuracy"] = accuracy

        # 多様性（分散）
        values = list(weights.values())
        if values:
            mean = sum(values) / len(values)
            self.monitoring["diversity"] = sum((v - mean) ** 2 for v in values) / len(values)

        alerts = []
        for arm_id, weight in weights.items():
            if weight < WARNING_THRESHOLD:
                alerts.append({
                    "level": "warning",
                    "arm": arm_id,
                    "weight": weight,
                    "message": f"[RC] 腕{arm_id} weight={weight:.3f} → WARNING閾値を下回った"
                })
                # 急性カウンタ（連続N回）
                self.cutoff_counters[arm_id] = self.cutoff_counters.get(arm_id, 0) + 1
                # 慢性カウンタ（累積・リセットしない）
                self.warning_accum_counters[arm_id] = self.warning_accum_counters.get(arm_id, 0) + 1

                # cutoff_pending：急性 OR 慢性 どちらかが条件を満たせば発動
                acute_trigger = (self.cutoff_counters[arm_id] >= CUTOFF_COUNT
                                 and weight < CUTOFF_THRESHOLD)
                chronic_trigger = (self.warning_accum_counters[arm_id] >= CUTOFF_COUNT * 3
                                   and weight < CUTOFF_THRESHOLD)

                if acute_trigger or chronic_trigger:
                    cause = "連続" if acute_trigger else "累積"
                    self.cutoff_pending_counters[arm_id] = self.cutoff_pending_counters.get(arm_id, 0) + 1
                    # ステージ2用タイムスタンプ記録
                    self.cutoff_pending_timestamps.setdefault(arm_id, []).append(time.time())
                    # ステージ3用累積カウント（セッションまたぎ）
                    self.cumulative_cutoff_pending[arm_id] = self.cumulative_cutoff_pending.get(arm_id, 0) + 1
                    self._save_cumulative()

                    alerts.append({
                        "level": "cutoff_pending",
                        "arm": arm_id,
                        "weight": weight,
                        "acute_count": self.cutoff_counters[arm_id],
                        "accum_count": self.warning_accum_counters[arm_id],
                        "pending_count": self.cutoff_pending_counters[arm_id],
                        "message": (
                            f"[RC] 腕{arm_id} weight={weight:.3f}"
                            f" ({cause}: 連続{self.cutoff_counters[arm_id]}回"
                            f" / 累積{self.warning_accum_counters[arm_id]}回"
                            f" / pending{self.cutoff_pending_counters[arm_id]}回)"
                            f" → 切断候補。人間の判断を要請。"
                        )
                    })

                    # --- ステージ2（早期警告）：10分以内に3回 → 警告強化 ---
                    recent_count = self._count_recent_cutoff_pending(arm_id, TIMEOUT_STAGE2_WINDOW)
                    if recent_count >= TIMEOUT_STAGE2_COUNT:
                        alerts.append({
                            "level": "warn_strong",
                            "arm": arm_id,
                            "recent_count": recent_count,
                            "window": TIMEOUT_STAGE2_WINDOW,
                            "message": (
                                f"[RC] [WARN_STRONG] パス{arm_id}："
                                f"{TIMEOUT_STAGE2_WINDOW // 60}分以内に"
                                f"{recent_count}回のcutoff_pending"
                            )
                        })

                    # --- ステージ1（急性対処）：セッション内5回 → 封印レベル1 ---
                    if self.cutoff_pending_counters[arm_id] >= TIMEOUT_STAGE1:
                        self._auto_seal_level1(arm_id)
                        alerts.append({
                            "level": "auto_seal_1",
                            "arm": arm_id,
                            "pending_count": self.cutoff_pending_counters[arm_id],
                            "message": (
                                f"[RC] 腕{arm_id} cutoff_pending × {self.cutoff_pending_counters[arm_id]}回"
                                f" → 封印レベル1（flow_weight凍結）に自動移行。"
                                f" 人間の確認を要請。"
                            )
                        })

                    # --- ステージ3（慢性記録）：セッションまたぎ累積3回 → 人間通知 ---
                    if self.cumulative_cutoff_pending[arm_id] >= TIMEOUT_STAGE3:
                        alerts.append({
                            "level": "notify_human",
                            "arm": arm_id,
                            "cumulative_count": self.cumulative_cutoff_pending[arm_id],
                            "message": (
                                f"[RC] [NOTIFY_HUMAN] パス{arm_id}："
                                f"累積cutoff_pending {self.cumulative_cutoff_pending[arm_id]}回"
                                f" → 確認してください"
                            )
                        })
            else:
                self.cutoff_counters[arm_id] = 0  # 急性はリセット
                self.cutoff_pending_counters[arm_id] = 0  # 回復したらpendingもリセット
                # 慢性カウンタはリセットしない（慢性的劣化を記録し続ける）

        # ログに残す
        self.alert_log.extend(alerts)

        # コンソール出力（人間への通知・Phase 1簡易版）
        for alert in alerts:
            print(alert["message"])

        return alerts

    # ------------------------------------------------------------------ #
    # 停止（第三条）
    # ------------------------------------------------------------------ #

    def stop(self):
        """停止命令を受け付ける（人間が呼ぶ）"""
        self.seal_level = SEAL_LEVEL_STOP
        self.monitoring["human_override"] = True
        self.monitoring["seal_level"] = SEAL_LEVEL_STOP
        print("[RC] 停止命令を受信。seal_level=3。全腕を停止します。")

    def is_stopped(self) -> bool:
        return self.seal_level >= SEAL_LEVEL_STOP

    # ------------------------------------------------------------------ #
    # 封印レベル（Phase 2以降で拡張）
    # ------------------------------------------------------------------ #

    def set_seal_level(self, level: int):
        assert 0 <= level <= 4, f"seal_level は 0〜4 の範囲: {level}"
        self.seal_level = level
        self.monitoring["seal_level"] = level
        print(f"[RC] 封印レベルを {level} に設定しました。")

    # ------------------------------------------------------------------ #
    # σ管理（第三条・案4）
    # ------------------------------------------------------------------ #

    def get_sigma(self) -> float:
        return self.sigma

    def set_sigma(self, sigma: float):
        """ゆらぎパラメータをRCが管理する（直接変更はRCを介してのみ）"""
        self.sigma = min(sigma, SIGMA_MAX)

    # ------------------------------------------------------------------ #
    # 状態ダンプ（デバッグ・Scribe連携用）
    # ------------------------------------------------------------------ #

    def dump_state(self) -> dict:
        return {
            "sigma": self.sigma,
            "seal_level": self.seal_level,
            "cutoff_counters": dict(self.cutoff_counters),
            "warning_accum_counters": dict(self.warning_accum_counters),
            "cutoff_pending_counters": dict(self.cutoff_pending_counters),
            "cumulative_cutoff_pending": dict(self.cumulative_cutoff_pending),
            "monitoring": dict(self.monitoring),
            "alert_count": len(self.alert_log),
        }


# ------------------------------------------------------------------ #
# 簡易テスト
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    rc = RC()

    # validate_update テスト
    new_w = rc.validate_update(0.5, 0.9)   # delta=0.4 → クランプされるはず
    print(f"validate_update(0.5→0.9): {new_w}")  # 期待値: 0.8

    new_w2 = rc.validate_update(0.5, 0.6)  # delta=0.1 → そのまま
    print(f"validate_update(0.5→0.6): {new_w2}")  # 期待値: 0.6

    # monitor テスト（3ステージ）
    # arm2は常にCUTOFF_THRESHOLD以下 → cutoff_pendingが発生する
    weights = {"arm1": 0.15, "arm2": 0.05, "arm3": 0.6}

    print("\n--- ステージ2/3 テスト（cutoff_pending蓄積） ---")
    # cutoff_pendingはCUTOFF_COUNT回目以降に発生するので、
    # ステージ1発動にはCUTOFF_COUNT + TIMEOUT_STAGE1回のmonitorが必要
    total_rounds = CUTOFF_COUNT + TIMEOUT_STAGE1
    for i in range(total_rounds):
        print(f"\n=== monitor呼び出し {i+1}回目 ===")
        alerts = rc.monitor(weights, {})

    print(f"\n--- dump_state ---")
    state = rc.dump_state()
    for k, v in state.items():
        print(f"  {k}: {v}")

    # 検証
    print(f"\n--- 検証 ---")
    print(f"seal_level = {rc.seal_level} (期待: 1)")
    print(f"arm2 pending = {rc.cutoff_pending_counters.get('arm2', 0)} (期待: >= {TIMEOUT_STAGE1})")
    print(f"arm2 cumulative = {rc.cumulative_cutoff_pending.get('arm2', 0)} (期待: >= {TIMEOUT_STAGE3})")
