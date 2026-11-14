"""A daily window for free-activity opportunities; never changes their owner switch."""
import datetime as dt
import math
import re

EXTRA_SLOT_GRACE_SECONDS = 300


class DailyWindow:
    def __init__(self,spec=None):
        self.spec=dict(spec) if spec else None
        if self.spec is None:return
        def minute(value):
            if not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):
                raise ValueError('autonomy_daily_window times must be HH:MM')
            hour,minutes=map(int,value.split(':'));return hour*60+minutes
        self.start=minute(self.spec.get('start'));self.end=minute(self.spec.get('end'))
        extra_times = self.spec.get('extra_times', [])
        if not isinstance(extra_times, list):
            raise ValueError('autonomy_daily_window extra_times must be a list')
        self.extra_minutes = sorted({minute(value) for value in extra_times})
        offset=self.spec.get('utc_offset_minutes',480)
        if type(offset)is not int or not -720<=offset<=840:raise ValueError('Invalid daily-window UTC offset')
        if self.start>=self.end:raise ValueError('Daily autonomy window needs start before end on the same day')
        self.zone=dt.timezone(dt.timedelta(minutes=offset))
        self.spec={'start':self.spec['start'],'end':self.spec['end'],'utc_offset_minutes':offset}
        if extra_times:
            self.spec['extra_times'] = [f'{value // 60:02d}:{value % 60:02d}' for value in self.extra_minutes]

    def bounds(self,when):
        day=dt.datetime.fromtimestamp(when,self.zone).replace(hour=0,minute=0,second=0,microsecond=0)
        return (day+dt.timedelta(minutes=self.start)).timestamp(),(day+dt.timedelta(minutes=self.end)).timestamp()

    def contains(self,when):
        if self.spec is None:return True
        start,end=self.bounds(when)
        return start<=when<end or any(
            slot<=when<slot+EXTRA_SLOT_GRACE_SECONDS for slot in self.extra_slots(start)
        )

    def extra_slots(self,window_start):
        midnight=window_start-self.start*60
        return [midnight+minute*60 for minute in self.extra_minutes]

    def constrain(self,when):
        if self.contains(when):return when
        return self.next_slot(when,3600,inclusive=True)

    def next_slot(self,when,interval,inclusive=False):
        if self.spec is None:return when+interval
        if type(interval)is not int or interval<120:raise ValueError('Autonomy interval needs at least 120 seconds')
        start,end=self.bounds(when)
        hops=math.ceil((when-start)/interval) if inclusive else math.floor((when-start)/interval)+1
        candidate=start+max(0,hops)*interval
        slots=self.extra_slots(start)
        future=[slot for slot in slots if slot>when or (inclusive and slot==when)]
        if candidate<end:
            future.append(candidate)
        return min(future) if future else min([start,*slots])+86400

    def view(self):
        return dict(self.spec) if self.spec else None
