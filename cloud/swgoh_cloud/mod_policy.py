"""A mod is eligible only when the game's secondary stats positively prove it."""
import math

def is_mod(shop,reward):
    reward=reward or {}
    return shop=='SHIPMENTMOD' or str(reward.get('type','')).replace('_','')=='STATMOD' or ':STATMOD_' in str(reward.get('definition',''))

def eligible_mod(reward):
    mod=(reward or {}).get('mod')
    if not isinstance(mod,dict) or mod.get('verified') is not True or type(mod.get('stars')) is not int or mod['stars']!=5:
        return False
    stats=mod.get('secondaries')
    if not isinstance(stats,list) or len(stats)>4:return False
    return any(isinstance(s,dict) and s.get('stat')=='SPEED' and type(s.get('value')) in (int,float) and math.isfinite(s['value']) and s['value']>0 for s in stats)
