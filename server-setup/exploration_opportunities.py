"""Persisted local dice for optional exploration; scheduled work never uses it."""
import json
import random


class ExplorationOpportunities:
    def __init__(self, runtime):
        self.r = runtime

    def consider(self, db, now, last_wake, last_user):
        config = self.r.settings.get('exploration_opportunities', {})
        if config.get('enabled') is not True:
            return None
        interval = float(config.get('interval_seconds', 1800))
        if interval <= 0:
            interval = 1800
        last = float(self.r._get(db, 'exploration_last_check', '0'))
        if now - max(last, last_wake) < interval:
            return {'due': False, 'reason': 'not_due'}
        misses = int(self.r._get(db, 'exploration_misses', '0'))
        base = float(config.get('probability', 0.5))
        bonus = min(float(config.get('max_bonus', 0.3)), misses * float(config.get('miss_bonus', 0.1)))
        age = now - last_user
        factor = 0.35 if age < 20 * 60 else 1.25 if age > 6 * 3600 else 1
        probability = max(0, min(1, base * factor + bonus))
        value = random.random()
        hit = value < probability
        result = {'due': hit, 'reason': 'hit' if hit else 'miss', 'at': now,
                  'probability': probability, 'roll': value, 'misses_before': misses}
        self.r._set(db, 'exploration_last_check', now)
        self.r._set(db, 'exploration_misses', 0 if hit else misses + 1)
        self.r._set(db, 'exploration_last_decision', json.dumps(result))
        return result
