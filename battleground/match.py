"""跨服战场与匹配系统 - 含7个bug"""
import random
from dataclasses import dataclass, field

@dataclass
class Player:
    pid: str
    rating: int
    class_type: str  # healer/tank/dps
    team: str = ""

class MatchSystem:
    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self.queue: list[Player] = []
        self.teams: dict[str, list[Player]] = {"A": [], "B": []}
        self.match_rating: dict[str, int] = {}  # player -> 匹配用rating
        self.leave_penalty: dict[str, int] = {}  # player -> 扣分次数
        self.balance_buff: dict[str, float] = {}  # team -> buff值
        self.reward_claimed: set = set()  # bug: 奖励去重

    def find_match(self, player: Player) -> tuple[bool, str]:
        # bug1: 只看rating不看职业
        self.queue.append(player)
        if len(self.queue) >= 10:
            return True, "matched"
        return False, "waiting"

    def replace_player(self, team: str, rejected: Player, new_player: Player) -> bool:
        # bug2: 有人拒绝全队重排
        return False  # 总是重排

    def get_balance_buff(self, team: str, score_diff: int) -> float:
        # bug3: 固定buff
        return 0.1

    def sync_cross_server(self, event: str, server: str):
        # bug4: 定时拉取
        pass

    def claim_reward(self, match_id: str, player: str) -> dict:
        # bug5: 无幂等
        return {"gold": 100}

    def calculate_team_rating(self, players: list[Player]) -> int:
        # bug6: 只看队长rating
        if not players:
            return 0
        return players[0].rating

    def penalize_leave(self, player: str):
        # bug7: 无惩罚
        pass
