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
"""

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

# cutoff_pendingタイムアウト（Phase 2最優先）
CUTOFF_PENDING_TIMEOUT = 10  # N回通知して無応答 → 封印レベル1へ自動移行


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
        self.cutoff_pending_counters: dict = {} # arm_id → cutoff_pending連続回数（タイムアウト用）
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
                    # タイムアウト：N回通知して無応答 → 封印レベル1へ自動移行
                    if (self.cutoff_pending_counters[arm_id] >= CUTOFF_PENDING_TIMEOUT
                            and self.seal_level == SEAL_LEVEL_NORMAL):
                        self.seal_level = 1
                        self.monitoring["seal_level"] = 1
                        alerts.append({
                            "level": "auto_seal_1",
                            "arm": arm_id,
                            "pending_count": self.cutoff_pending_counters[arm_id],
                            "message": (
                                f"[RC] 腕{arm_id} cutoff_pending × {self.cutoff_pending_counters[arm_id]}回"
                                f" 無応答 → 封印レベル1（flow_weight凍結）に自動移行。"
                                f" 人間の確認を要請。"
                            )
                        })
                        print(f"[RC] ⚠️  封印レベル1に自動移行。seal_level=1")
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

    # monitor テスト（警告・切断候補）
    weights = {"arm1": 0.15, "arm2": 0.05, "arm3": 0.6}
    for _ in range(3):  # 3回連続でarm2が低い
        alerts = rc.monitor(weights, {})

    print(f"\ndump_state: {rc.dump_state()}")
