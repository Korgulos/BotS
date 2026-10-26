"""Rank reward claims: public preview only; native references stay on the device."""
import hashlib,json,re

def message_hash(message):
    return hashlib.sha256(json.dumps({k:v for k,v in message.items() if k!='claimReference'},sort_keys=True,separators=(',',':')).encode()).hexdigest()

def preview(message):
    kind=str(message.get('type','')).replace('_','')
    if kind=='RANKREWARDS':kind='RANKREWARD'
    if kind not in {'RANKREWARD','TERRITORYTOURNAMENTDAILYRANKREWARD'} and (kind,message.get('subjectKey')) not in {('TERRITORYTOURNAMENTMATCHREWARD','TERRITORYTOURNAMENT_REWARD_NO_CHANGE_FIRST_SUBJECT_KEY'),('PARAMETERIZED','RAID_REWARD_SUBJECT_KEY'),('GUILDRANKEDREWARD','GUILD_CHALLENGE_REWARD_SUBJECT_KEY'),('TERRITORYWARCOMPLETEREWARD','TERRITORY_WAR_SECOND_PLACE_SUBJECT_KEY')}:raise ValueError('Unsupported inbox type and subject')
    if any(message.get(k) for k in ('hasUnverifiedLiveDetails','hasEntitlement','hasCoupon','hasActionLink','modCount','customButtonKey','attachmentsTruncated','fromSupport')):raise ValueError('Special inbox flow')
    items=message.get('attachments',[])
    if not 0<len(items)<=32 or message.get('attachmentCount')!=len(items):raise ValueError('Incomplete attachments')
    rewards=[]
    for i in items:
        n=i.get('minQuantity');d=i.get('definition','');t=i.get('type')
        if type(n) is not int or n<=0 or n!=i.get('maxQuantity') or t not in {'CURRENCY','MATERIAL','EQUIPMENT'} or not re.fullmatch(r'[A-Za-z0-9_:./-]{1,160}',d):raise ValueError('Unsupported reward')
        rewards.append(dict(type=t,definition=d,quantity=n))
    return dict(kind='inbox',type=kind,subjectKey=message.get('subjectKey',''),starts=str(int(message['starts'])),expires=str(int(message['expires'])),rewards=sorted(rewards,key=lambda x:(x['type'],x['definition'],x['quantity'])))

def native_preview(quote):
    q={k:v for k,v in quote.items() if k!='reference'}
    q['kind']='inbox';q['rewards']=sorted(q['rewards'],key=lambda x:(x['type'],x['definition'],x['quantity']))
    return q
