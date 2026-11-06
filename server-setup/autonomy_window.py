"""A daily window for free-activity opportunities; never changes their owner switch."""
import datetime as dt
import math
import re


class DailyWindow:
    def __init__(self,spec=None):
        self.spec=dict(spec) if spec else None
        if self.spec is None:return
        def minute(key):
            value=self.spec.get(key,'')
            if not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):
                raise ValueError('autonomy_daily_window '+key+' must be HH:MM')
            hour,minutes=map(int,value.split(':'));return hour*60+minutes
        self.start=minute('start');self.end=minute('end')
        offset=self.spec.get('utc_offset_minutes',480)
        if type(offset)is not int or not -720<=offset<=840:raise ValueError('Invalid daily-window UTC offset')
        if self.start>=self.end:raise ValueError('Daily autonomy window needs start before end on the same day')
        self.zone=dt.timezone(dt.timedelta(minutes=offset))
        self.spec={'start':self.spec['start'],'end':self.spec['end'],'utc_offset_minutes':offset}

    def bounds(self,when):
        day=dt.datetime.fromtimestamp(when,self.zone).replace(hour=0,minute=0,second=0,microsecond=0)
        return (day+dt.timedelta(minutes=self.start)).timestamp(),(day+dt.timedelta(minutes=self.end)).timestamp()

    def contains(self,when):
        if self.spec is None:return True
        start,end=self.bounds(when);return start<=when<end

    def constrain(self,when):
        if self.spec is None:return when
        start,end=self.bounds(when)
        if when<start:return start
        if when>=end:return start+86400
        return when

    def next_slot(self,when,interval,inclusive=False):
        if self.spec is None:return when+interval
        if type(interval)is not int or interval<120:raise ValueError('Autonomy interval needs at least 120 seconds')
        start,end=self.bounds(when)
        if when<start:return start
        hops=math.ceil((when-start)/interval) if inclusive else math.floor((when-start)/interval)+1
        candidate=start+max(0,hops)*interval
        return candidate if candidate<end else start+86400

    def view(self):
        return dict(self.spec) if self.spec else None
