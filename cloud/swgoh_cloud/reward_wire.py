"""Verified reward-only inbox ACCEPT and empty free-energy claim codec."""
import json
from pathlib import Path
from .territory_wire import Codec
from .session_wire import fields,build_request,initial_projection
from .direct_wire import field
from .shipment_wire import receive
from .inbox_policy import preview

class Rewards(Codec):
    def __init__(self):super().__init__(json.loads(Path(__file__).with_name('reward_schema.json').read_text()))
    def enum(self,name,value):return self.schema['_enums'][name].get(str(value),'UNKNOWN')
    def inbox(self,payload):
        inbox=self.child(payload,'InitialDataResponse','inbox');out=[];ids=set()
        for blob in self.children(inbox,'Inbox','message',100):
            s=lambda n,d=None:self.scalar(blob,'InboxMessage',n,d)
            identifier=s('id')
            if not identifier or identifier in ids:raise ValueError('Invalid inbox identifiers')
            ids.add(identifier);attachments=[]
            for b in self.values(blob,'InboxMessage','attachment')[:32]:
                attachments.append(dict(type=self.enum('ItemType',self.scalar(b,'BucketItem','type',1)),definition=self.scalar(b,'BucketItem','id'),minQuantity=self.scalar(b,'BucketItem','minQuantity',0),maxQuantity=self.scalar(b,'BucketItem','maxQuantity',0)))
            message=dict(type=self.enum('InboxMessageType',s('type',1)),subjectKey=s('subject_key'),starts=str(s('start_time',0)),expires=str(s('expire_time',0)),attachments=attachments,attachmentCount=len(self.values(blob,'InboxMessage','attachment')),attachmentsTruncated=len(self.values(blob,'InboxMessage','attachment'))>32,fromSupport=bool(s('from_support',0)),customButtonKey=s('custom_button_key'),modCount=len(self.values(blob,'InboxMessage','stat_mod')),hasEntitlement=bool(self.values(blob,'InboxMessage','claimable_entitlement')),hasCoupon=bool(self.values(blob,'InboxMessage','store_coupon')),hasActionLink=bool(self.values(blob,'InboxMessage','action_link') or self.values(blob,'InboxMessage','custom_button_link')))
            out.append((identifier,s('type',1),message))
        return out
    def bonus_times(self,payload,player_only=False):
        player=payload if player_only else self.child(payload,'InitialDataResponse','player')
        rows=[dict(type=self.scalar(b,'EnergyBonusTime','energy_type',1),last=str(self.scalar(b,'EnergyBonusTime','last_bonus_energy_time',0))) for b in self.children(player,'Player','last_energy_bonus_time',20)]
        return sorted(rows,key=lambda r:r['type'])
    def packet(self,session,operation,message=None):
        if operation=='UpdateInbox':
            identifier,kind,document=message;preview(document)
            if self.enum('InboxAction',0)!='ACCEPT':raise ValueError('ACCEPT schema mismatch')
            body=field(self.tag('UpdateInboxRequest','message_id'),identifier)+field(self.tag('UpdateInboxRequest','action'),0)+field(self.tag('UpdateInboxRequest','message_type'),kind)
        elif operation=='ClaimAllEnergyRewards':
            if self.schema['ClaimAllEnergyRewardsRequest']:raise ValueError('Energy request schema changed')
            body=b''
        else:raise ValueError('Reward operation is not allowlisted')
        envelope=fields(build_request(session,'Ping'));envelope[4]=[b'PlayerRpc'];envelope[5]=[operation.encode()];envelope[6]=[body]
        return b''.join(field(t,v) for t,values in envelope.items() for v in values)
    def initial(self,session,client):
        code,payload=receive(client,build_request(session,'GetInitialData'))
        if code!=1:raise ValueError('Session rejected')
        if initial_projection(payload)['player']['allyCode']!=session['expectedAllyCode']:raise ValueError('Account mismatch')
        return payload
