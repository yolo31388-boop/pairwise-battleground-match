"""跨服战场与匹配系统。

修复点:
1. 匹配同时考虑战力(rating)与职业搭配, 每队必须有治疗, 缺治疗时优先补治疗。
2. 有人拒绝时只替换该玩家, 其余玩家保留; 补人等待有超时, 超时才全队重排。
3. 平衡性buff随比分差距动态缩放, 差距越大buff越强, 且有上限。
4. 跨服数据增量推送, 关键事件(首杀/开团/结束等)即时同步。
5. 奖励发放以(场次, 玩家)为唯一标识, 幂等处理, 重复领取不重复发奖。
6. 组队匹配用全队平均rating, 并支持队长权重(默认权重1, 即纯平均)。
7. 中途退出扣分+冷却, 连续退出惩罚逐级加重。
"""
import random
import time
from dataclasses import dataclass, field

TEAM_SIZE = 5

# 匹配门槛
REQUIRED_HEALERS_PER_TEAM = 1
MAX_RATING_SPREAD = 500        # 同队最大评分跨度
MAX_TEAM_RATING_GAP = 100      # 两队平均评分最大差

# 拒绝后补人
REPLACE_TIMEOUT = 30.0         # 补人等待超时(秒)

# 平衡性buff: 落后方按人头差获得属性加成
BASE_BUFF = 0.0
BUFF_PER_SCORE = 0.005         # 每落后1个人头加0.5%
MAX_BALANCE_BUFF = 0.5         # 最高50%

# 中途退出惩罚
LEAVE_BASE_PENALTY = 50        # 首次退出扣50
LEAVE_PENALTY_STEP = 50        # 每多一次多扣50
LEAVE_MAX_PENALTY = 300
LEAVE_BASE_COOLDOWN = 300      # 首次冷却5分钟
LEAVE_COOLDOWN_STEP = 300      # 每多一次加5分钟
LEAVE_MAX_COOLDOWN = 1800

# 关键事件: 必须即时推送到其他服
KEY_EVENTS = frozenset({
    "first_blood", "match_start", "match_end", "victory",
    "base_capture", "boss_kill", "objective",
})


@dataclass
class Player:
    pid: str
    rating: int
    class_type: str  # healer/tank/dps
    team: str = ""


class MatchSystem:
    def __init__(self, seed: int = 42, time_func=time.monotonic):
        self.rng = random.Random(seed)
        self.queue: list[Player] = []
        self.teams: dict[str, list[Player]] = {"A": [], "B": []}
        self.match_rating: dict[str, int] = {}  # player -> 匹配用rating
        self.leave_penalty: dict[str, int] = {}  # player -> 退出次数
        self.leave_points_lost: dict[str, int] = {}  # player -> 累计扣分
        self.leave_cooldown_until: dict[str, float] = {}  # player -> 冷却截止时间
        self.balance_buff: dict[str, float] = {}  # team -> buff值
        self.reward_claimed: set = set()  # (match_id, player) 已发奖唯一标识

        # 拒绝/掉线后补人: (team, slot_player) -> 截止时间
        self.pending_replacements: dict[tuple[str, str], float] = {}

        # 跨服同步: 增量事件流 + 各服同步位点
        self.sync_events: list[dict] = []
        self.sync_offsets: dict[str, int] = {}
        self.immediate_events: list[dict] = []  # 关键事件即时推送记录
        self.time_func = time_func

    # ---------- bug1: 战力 + 职业搭配 ----------
    def find_match(self, player: Player) -> tuple[bool, str]:
        if player not in self.queue and not any(
                p.pid == player.pid for p in self.queue):
            self.queue.append(player)

        if len(self.queue) < TEAM_SIZE * 2:
            return False, "waiting"

        healers = [p for p in self.queue if p.class_type == "healer"]
        if len(healers) < REQUIRED_HEALERS_PER_TEAM * 2:
            # 缺治疗: 保持等待, 治疗进队时优先安排
            return False, "waiting_for_healer"

        chosen = self._select_players()
        if chosen is None:
            return False, "rating_spread_too_large"

        team_a, team_b = chosen
        gap = abs(self._average_rating(team_a) - self._average_rating(team_b))
        if gap > MAX_TEAM_RATING_GAP:
            return False, "teams_unbalanced"

        for p in team_a + team_b:
            self.queue.remove(p)
        self.teams["A"] = team_a
        self.teams["B"] = team_b
        for p in team_a + team_b:
            self.match_rating[p.pid] = p.rating
        return True, "matched"

    def _select_players(self) -> tuple[list[Player], list[Player]] | None:
        """从队列中选出10人: 先锁治疗(优先补治疗), 再按战力接近补齐。"""
        pool = list(self.queue)
        ordered = sorted(pool, key=lambda p: p.rating)
        mid = len(ordered) // 2
        anchor = ordered[mid].rating

        # 治疗优先: 缺治疗时评分最接近锚点的治疗先入选
        healers = sorted(
            (p for p in pool if p.class_type == "healer"),
            key=lambda p: abs(p.rating - anchor),
        )
        chosen = healers[:REQUIRED_HEALERS_PER_TEAM * 2]
        chosen_ids = {p.pid for p in chosen}

        others = sorted(
            (p for p in pool if p.pid not in chosen_ids),
            key=lambda p: abs(p.rating - anchor),
        )
        chosen.extend(others[:TEAM_SIZE * 2 - len(chosen)])

        if max(p.rating for p in chosen) - min(p.rating for p in chosen) \
                > MAX_RATING_SPREAD:
            return None

        healer_a = [chosen[0]]
        healer_b = [chosen[1]]
        rest = [p for p in chosen if p not in healer_a + healer_b]
        rest.sort(key=lambda p: p.rating, reverse=True)

        # 蛇形分配, 尽量拉平两队平均战力
        team_a, team_b = list(healer_a), list(healer_b)
        for i, p in enumerate(rest):
            (team_a if i % 2 == 0 else team_b).append(p)
        return team_a, team_b

    @staticmethod
    def _average_rating(players: list[Player]) -> float:
        if not players:
            return 0.0
        return sum(p.rating for p in players) / len(players)

    # ---------- bug2: 只补人, 不全队重排 ----------
    def mark_pending_replacement(self, team: str, rejected: Player) -> float:
        """登记一个待补位置, 返回等待截止时间戳。"""
        deadline = self.time_func() + REPLACE_TIMEOUT
        self.pending_replacements[(team, rejected.pid)] = deadline
        return deadline

    def replacement_expired(self, team: str, rejected: Player) -> bool:
        """补人等待是否超时; 超时由调用方触发全队重排。"""
        deadline = self.pending_replacements.get((team, rejected.pid))
        return deadline is None or self.time_func() >= deadline

    def replace_player(self, team: str, rejected: Player,
                       new_player: Player | None = None) -> bool:
        """只替换拒绝/掉线的那一个玩家, 其余队员保留。

        没有候选人时登记补人等待并返回False(尚未补齐, 但不全队重排);
        等待超时(replacement_expired)才允许全队重排。
        """
        roster = self.teams.get(team)
        if roster is None or rejected not in roster:
            return False

        if new_player is None:
            self.mark_pending_replacement(team, rejected)
            return False

        idx = roster.index(rejected)
        rejected.team = ""
        new_player.team = team
        roster[idx] = new_player
        self.match_rating.pop(rejected.pid, None)
        self.match_rating[new_player.pid] = new_player.rating
        self.pending_replacements.pop((team, rejected.pid), None)
        return True

    # ---------- bug3: 动态平衡性buff ----------
    def get_balance_buff(self, team: str, score_diff: int) -> float:
        """score_diff为该队落后的人头数; 落后越多加成越高, 封顶。"""
        if score_diff <= 0:
            buff = 0.0
        else:
            buff = min(
                MAX_BALANCE_BUFF,
                BASE_BUFF + score_diff * BUFF_PER_SCORE,
            )
        self.balance_buff[team] = buff
        return round(buff, 3)

    # ---------- bug4: 增量推送 + 关键事件即时同步 ----------
    def sync_cross_server(self, event: str, server: str,
                          payload: dict | None = None) -> dict:
        """记录跨服事件并同步给其他服。

        关键事件即时广播; 普通事件写入增量日志, 其他服按位点增量拉取。
        返回同步结果, 含模式(push/incremental)与事件序号。
        """
        entry = {
            "seq": len(self.sync_events),
            "event": event,
            "server": server,
            "payload": payload or {},
            "ts": self.time_func(),
        }
        self.sync_events.append(entry)

        immediate = event in KEY_EVENTS
        if immediate:
            self.immediate_events.append(entry)
            mode = "push"
        else:
            mode = "incremental"
        return {
            "mode": mode,
            "immediate": immediate,
            "seq": entry["seq"],
            "event": event,
        }

    def pull_incremental(self, server: str) -> list[dict]:
        """其他服按上次同步位点增量拉取新事件。"""
        offset = self.sync_offsets.get(server, 0)
        events = self.sync_events[offset:]
        self.sync_offsets[server] = len(self.sync_events)
        return events

    # ---------- bug5: 奖励幂等 ----------
    def claim_reward(self, match_id: str, player: str,
                     reward: dict | None = None) -> dict:
        """以(场次, 玩家)为唯一标识发奖, 重复领取返回空结果。"""
        key = (match_id, player)
        if key in self.reward_claimed:
            return {}
        self.reward_claimed.add(key)
        return dict(reward) if reward is not None else {"gold": 100}

    # ---------- bug6: 全队平均 + 队长权重 ----------
    def calculate_team_rating(self, players: list[Player],
                              leader_weight: float = 1.0) -> int:
        """组队匹配评分 = 加权平均(队长权重leader_weight, 其余队员权重1)。

        leader_weight=1时就是全队纯平均, 不再只看队长一个人。
        """
        if not players:
            return 0
        if len(players) == 1:
            return players[0].rating
        leader = players[0]
        members = players[1:]
        total_weight = leader_weight + len(members)
        weighted = leader.rating * leader_weight + sum(
            p.rating for p in members)
        return round(weighted / total_weight)

    # ---------- bug7: 中途退出惩罚 ----------
    def penalize_leave(self, player: str) -> dict:
        """中途退出扣分并进入匹配冷却; 连续退出惩罚逐级加重。"""
        count = self.leave_penalty.get(player, 0) + 1
        self.leave_penalty[player] = count

        points = min(
            LEAVE_MAX_PENALTY,
            LEAVE_BASE_PENALTY + (count - 1) * LEAVE_PENALTY_STEP,
        )
        cooldown = min(
            LEAVE_MAX_COOLDOWN,
            LEAVE_BASE_COOLDOWN + (count - 1) * LEAVE_COOLDOWN_STEP,
        )
        self.leave_points_lost[player] = (
            self.leave_points_lost.get(player, 0) + points)
        self.leave_cooldown_until[player] = self.time_func() + cooldown
        return {
            "leave_count": count,
            "points_lost": points,
            "cooldown": cooldown,
        }

    def in_leave_cooldown(self, player: str) -> bool:
        until = self.leave_cooldown_until.get(player)
        return until is not None and self.time_func() < until
