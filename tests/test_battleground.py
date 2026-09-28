"""跨服战场与匹配系统 - 红态测试"""
import pytest, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from battleground.match import MatchSystem, Player

class TestClassBalance:
    def test_match_considers_class(self):
        ms = MatchSystem()
        # 5个dps排队，应该等治疗而不是直接开
        for i in range(5):
            ms.queue.append(Player(f"p{i}", 1500, "dps"))
        result, msg = ms.find_match(Player("p5", 1500, "dps"))
        # 6个dps没有治疗，不应该匹配成功（或者匹配但平衡性差）
        # 这里验证：匹配时会检查职业搭配
        # 当前实现只看人数，这个测试需要系统有职业检查
        assert "healer" in msg or result == False  # bug1: 直接matched

class TestReplaceNotRematch:
    def test_rejected_player_replaced_only(self):
        ms = MatchSystem()
        p1 = Player("p1", 1500, "dps")
        p2 = Player("p2", 1500, "healer")
        new_p = Player("p3", 1500, "dps")
        ms.teams["A"] = [p1, p2]
        result = ms.replace_player("A", p1, new_p)
        assert result == True  # bug2: 返回False（全队重排）
        assert p1 not in ms.teams["A"]
        assert new_p in ms.teams["A"]

class TestBalanceBuffScaling:
    def test_buff_scales_with_score_gap(self):
        ms = MatchSystem()
        b1 = ms.get_balance_buff("A", 1)
        b50 = ms.get_balance_buff("A", 50)
        assert b50 > b1  # bug3: 都是0.1

class TestRewardIdempotent:
    def test_reward_cannot_claim_twice(self):
        ms = MatchSystem()
        r1 = ms.claim_reward("m1", "p1")
        r2 = ms.claim_reward("m1", "p1")
        assert r2 == {} or r2.get("gold", 0) == 0  # bug5: 又领了100

class TestTeamRatingAverage:
    def test_team_rating_is_average(self):
        ms = MatchSystem()
        players = [Player("p1", 2000, "dps"), Player("p2", 1000, "healer")]
        avg = ms.calculate_team_rating(players)
        assert avg == 1500  # bug6: 只看p1返回2000

class TestLeavePenalty:
    def test_leaver_gets_penalty(self):
        ms = MatchSystem()
        ms.penalize_leave("p1")
        assert ms.leave_penalty.get("p1", 0) > 0  # bug7: 无惩罚
