"""跨服战场与匹配系统"""
import time
import random
from dataclasses import dataclass, field

TEAM_SIZE = 5
MATCH_SIZE = TEAM_SIZE * 2
# 关键事件：即时同步，不等增量批次
KEY_EVENTS = {"first_blood", "match_end", "objective_captured"}


@dataclass
class Player:
    pid: str
    rating: int
    class_type: str  # healer/tank/dps
    team: str = ""


class MatchSystem:
    # 补人等待超时（秒），超时后才全队重排
    REPLACE_TIMEOUT = 120
    # 平衡性buff：基础值 + 每分差系数，封顶
    BUFF_BASE = 0.05
    BUFF_PER_POINT = 0.01
    BUFF_CAP = 0.50
    # 中途退出：扣分与冷却，连续退出指数加重
    LEAVE_RATING_PENALTY = 25
    LEAVE_COOLDOWN_BASE = 300  # 秒

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self.queue: list[Player] = []
        self.teams: dict[str, list[Player]] = {"A": [], "B": []}
        self.match_rating: dict[str, int] = {}  # player -> 匹配用rating
        self.leave_penalty: dict[str, int] = {}  # player -> 退出次数（连续退出加重）
        self.leave_cooldown_until: dict[str, float] = {}  # player -> 冷却截止
        self.balance_buff: dict[str, float] = {}  # team -> buff值
        self.reward_claimed: set = set()  # (match_id, player) 幂等键
        # 跨服同步：事件日志 + 各服已同步偏移（增量推送）
        self.event_log: list[dict] = []
        self.server_offsets: dict[str, int] = {}
        self.pushed_events: dict[str, list[dict]] = {}

    # bug1: 匹配同时看战力和职业搭配，缺治疗时优先补治疗
    def find_match(self, player: Player) -> tuple[bool, str]:
        self.queue.append(player)
        if len(self.queue) < MATCH_SIZE:
            healers = sum(1 for p in self.queue if p.class_type == "healer")
            if healers == 0:
                return False, "waiting: need healer"
            return False, "waiting"
        return self._form_teams()

    def _form_teams(self) -> tuple[bool, str]:
        healers = [p for p in self.queue if p.class_type == "healer"]
        if not healers:
            return False, "waiting: need healer"
        # 按战力排序后蛇形分边，并把治疗/坦克均匀分到两队
        by_class = {"healer": [], "tank": [], "dps": []}
        for p in sorted(self.queue, key=lambda p: -p.rating):
            by_class.setdefault(p.class_type, by_class["dps"]).append(p)
        teams = {"A": [], "B": []}
        for cls in ("healer", "tank", "dps"):
            for i, p in enumerate(by_class.get(cls, [])):
                side = "A" if i % 2 == 0 else "B"
                teams[side].append(p)
        if len(teams["A"]) < TEAM_SIZE or len(teams["B"]) < TEAM_SIZE:
            return False, "waiting: unbalanced classes"
        self.teams = {s: ps[:TEAM_SIZE] for s, ps in teams.items()}
        for side, ps in self.teams.items():
            for p in ps:
                p.team = side
        self.queue = self.queue[MATCH_SIZE:]
        return True, "matched"

    # bug2: 有人拒绝时只补人，不全队重排；补人等待有超时
    def replace_player(self, team: str, rejected: Player, new_player: Player,
                       waited_seconds: float = 0) -> bool:
        if waited_seconds > self.REPLACE_TIMEOUT:
            return False  # 补人超时，才触发全队重排
        members = self.teams.get(team, [])
        for i, p in enumerate(members):
            if p.pid == rejected.pid:
                new_player.team = team
                members[i] = new_player
                return True
        return False

    # bug3: 平衡性buff按比分差距动态缩放，差距越大buff越强
    def get_balance_buff(self, team: str, score_diff: int) -> float:
        diff = abs(score_diff)
        buff = min(self.BUFF_CAP, self.BUFF_BASE + self.BUFF_PER_POINT * diff)
        self.balance_buff[team] = buff
        return buff

    # bug4: 跨服同步改为增量推送，关键事件即时同步
    def sync_cross_server(self, event: str, server: str,
                          others: list[str] | None = None) -> dict[str, list[dict]]:
        record = {"event": event, "server": server, "ts": time.time()}
        self.event_log.append(record)
        targets = others if others is not None else []
        pushed: dict[str, list[dict]] = {}
        for srv in targets:
            if srv == server:
                continue
            offset = self.server_offsets.get(srv, 0)
            delta = self.event_log[offset:]  # 只推增量，不全量拉取
            if event in KEY_EVENTS or delta:
                self.pushed_events.setdefault(srv, []).extend(delta)
                self.server_offsets[srv] = len(self.event_log)
                pushed[srv] = delta
        return pushed

    # bug5: 奖励发放按 (match_id, player) 唯一标识幂等
    def claim_reward(self, match_id: str, player: str) -> dict:
        key = (match_id, player)
        if key in self.reward_claimed:
            return {}
        self.reward_claimed.add(key)
        return {"gold": 100}

    # bug6: 队伍等级 = 全队平均；队长权重体现在匹配分上而非只看队长
    def calculate_team_rating(self, players: list[Player]) -> int:
        if not players:
            return 0
        return sum(p.rating for p in players) // len(players)

    def party_match_rating(self, players: list[Player], captain_weight: float = 0.2) -> int:
        """组队匹配分：全队平均 + 队长额外权重，防止高分队长拖低分队友炸鱼/被炸。"""
        if not players:
            return 0
        avg = self.calculate_team_rating(players)
        captain = players[0].rating
        return round(avg * (1 - captain_weight) + captain * captain_weight)

    # bug7: 中途退出扣分 + 冷却，连续退出指数加重
    def penalize_leave(self, player: str) -> dict:
        count = self.leave_penalty.get(player, 0) + 1
        self.leave_penalty[player] = count
        rating_loss = self.LEAVE_RATING_PENALTY * count
        cooldown = self.LEAVE_COOLDOWN_BASE * (2 ** (count - 1))
        self.leave_cooldown_until[player] = time.time() + cooldown
        self.match_rating[player] = max(
            0, self.match_rating.get(player, 1500) - rating_loss)
        return {"count": count, "rating_loss": rating_loss, "cooldown": cooldown}
