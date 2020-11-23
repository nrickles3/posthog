from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from dateutil.relativedelta import relativedelta
from django.db import connection
from django.utils import timezone

from posthog.constants import TREND_FILTER_TYPE_ACTIONS
from posthog.models.entity import Entity
from posthog.models.event import Event
from posthog.models.filter import Filter

LIFECYCLE_SQL = """
SELECT array_agg(day_start ORDER BY day_start ASC), array_agg(counts ORDER BY day_start ASC), status FROM  (
    SELECT (SUM(counts) :: int) as counts, day_start, status
    FROM (
             SELECT date_trunc(%(interval)s, %(after_date_to)s -
                                      n * INTERVAL %(one_interval)s) as day_start,
                    0                                               AS counts,
                    status
             from generate_series(1, %(num_intervals)s) as n
                      CROSS JOIN
                  (
                      SELECT status
                      FROM unnest(ARRAY ['new', 'returning', 'resurrecting', 'dormant']) status
                  ) as sec
             UNION ALL
             SELECT subsequent_day, count(DISTINCT person_id) counts, status
             FROM (
                      SELECT pdi.person_id, e.subsequent_day, e.status
                      FROM (
                               SELECT e.distinct_id,
                                      subsequent_day,
                                      CASE
                                          WHEN base_day = to_timestamp('0000-00-00 00:00:00', 'YYYY-MM-DD HH24:MI:SS')
                                              THEN 'dormant'
                                          WHEN subsequent_day = base_day + INTERVAL %(one_interval)s THEN 'returning'
                                          WHEN earliest < base_day THEN 'resurrecting'
                                          ELSE 'new'
                                          END as status
                               FROM (
                                        SELECT test.distinct_id, base_day, min(subsequent_day) as subsequent_day
                                        FROM (
                                                 SELECT events.distinct_id, day as base_day, sub_day as subsequent_day
                                                 FROM (
                                                          SELECT DISTINCT distinct_id,
                                                                          DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') AS "day"
                                                          FROM posthog_event
                                                          {action_join}
                                                          WHERE team_id = %(team_id)s
                                                            AND {event_condition}
                                                          GROUP BY distinct_id, day
                                                          HAVING DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') >=
                                                                 %(prev_date_from)s
                                                             AND DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') <=
                                                                 %(date_to)s
                                                      ) base
                                                          JOIN (
                                                     SELECT DISTINCT distinct_id,
                                                                     DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') AS "sub_day"
                                                     FROM posthog_event
                                                     {action_join}
                                                     WHERE team_id = %(team_id)s
                                                       AND {event_condition}
                                                     GROUP BY distinct_id, sub_day
                                                     HAVING DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') >=
                                                            %(prev_date_from)s
                                                        AND DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') <=
                                                            %(date_to)s
                                                 ) events ON base.distinct_id = events.distinct_id
                                                 WHERE sub_day > day
                                             ) test
                                        GROUP BY distinct_id, base_day
                                        UNION ALL
                                        SELECT distinct_id, min(day) as base_day, min(day) as subsequent_day
                                        FROM (
                                                 SELECT DISTINCT distinct_id,
                                                                 DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') AS "day"
                                                 FROM posthog_event
                                                 {action_join}
                                                 WHERE team_id = %(team_id)s
                                                   AND {event_condition}
                                                 GROUP BY distinct_id, day
                                                 HAVING DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') >=
                                                        %(prev_date_from)s
                                                    AND DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') <=
                                                        %(date_to)s
                                             ) base
                                        GROUP BY distinct_id
                                        UNION ALL
                                        SELECT distinct_id, base_day, subsequent_day
                                        FROM (
                                                 SELECT *
                                                 FROM (
                                                          SELECT *,
                                                                 LAG(distinct_id, 1) OVER ( ORDER BY distinct_id)    lag_id,
                                                                 LAG(subsequent_day, 1) OVER ( ORDER BY distinct_id) lag_day
                                                          FROM (
                                                                   SELECT distinct_id, total as base_day, day_start as subsequent_day
                                                                   FROM (
                                                                            SELECT DISTINCT distinct_id,
                                                                                            array_agg(date_trunc(%(interval)s, posthog_event.timestamp)) as day
                                                                            FROM posthog_event
                                                                            {action_join}
                                                                            WHERE team_id = %(team_id)s
                                                                              AND {event_condition}
                                                                              AND posthog_event.timestamp <= %(after_date_to)s
                                                                              AND DATE_TRUNC(%(interval)s, "posthog_event"."timestamp" AT TIME ZONE 'UTC') >=
                                                                                  %(date_from)s
                                                                            GROUP BY distinct_id
                                                                        ) as e
                                                                            CROSS JOIN (
                                                                       SELECT to_timestamp('0000-00-00 00:00:00', 'YYYY-MM-DD HH24:MI:SS') AS total,
                                                                              DATE_TRUNC(%(interval)s,
                                                                                         %(after_date_to)s -
                                                                                         n * INTERVAL %(one_interval)s) as day_start
                                                                       FROM generate_series(1, %(num_intervals)s) as n
                                                                   ) as b
                                                                   WHERE day_start != ALL (day)
                                                                   ORDER BY distinct_id, subsequent_day ASC
                                                               ) dormant_days
                                                               ORDER BY distinct_id, subsequent_day ASC
                                                      ) lagged
                                                 WHERE ((lag_id IS NULL OR lag_id != lagged.distinct_id) AND subsequent_day != %(date_from)s)
                                                    OR (lag_id = lagged.distinct_id AND lag_day < subsequent_day - INTERVAL %(one_interval)s)
                                             ) dormant_days
                                    ) e
                                        JOIN (
                                   SELECT DISTINCT distinct_id,
                                                   DATE_TRUNC(%(interval)s,
                                                              min("posthog_event"."timestamp") AT TIME ZONE 'UTC') earliest
                                   FROM posthog_event
                                   {action_join}
                                   WHERE team_id = %(team_id)s
                                     AND {event_condition}
                                   GROUP BY distinct_id
                               ) earliest ON e.distinct_id = earliest.distinct_id
                           ) e
                               JOIN
                           (SELECT person_id,
                                   distinct_id
                            FROM posthog_persondistinctid
                            WHERE team_id = %(team_id)s) pdi on e.distinct_id = pdi.distinct_id
                  ) grouped_counts
             WHERE subsequent_day <= %(date_to)s
               AND subsequent_day >= %(date_from)s
             GROUP BY subsequent_day, status
         ) counts
    GROUP BY day_start, status
    ) arrayified
GROUP BY status
"""

ACTION_JOIN = """
INNER JOIN posthog_action_events
ON posthog_event.id = posthog_action_events.event_id
"""


def get_interval(period: str) -> Union[timedelta, relativedelta]:
    if period == "minute":
        return timedelta(minutes=1)
    elif period == "day":
        return timedelta(days=1)
    elif period == "week":
        return timedelta(weeks=1)
    elif period == "month":
        return relativedelta(months=1)
    else:
        raise ValueError("{period} not supported".format(period))


def get_time_diff(
    interval: str, start_time: Optional[datetime], end_time: Optional[datetime], team_id: int
) -> Tuple[int, datetime, datetime, datetime, datetime]:

    _start_time = start_time or Event.objects.filter(team_id=team_id).order_by("timestamp")[0].timestamp.replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    _end_time = end_time or timezone.now()

    time_diffs: Dict[str, Any] = {
        "minute": 60,
        "hour": 3600,
        "day": 3600 * 24,
        "week": 3600 * 24 * 7,
        "month": 3600 * 24 * 30,
    }
    interval_diff = get_interval(interval)

    diff = _end_time - _start_time
    return (
        int(diff.total_seconds() / time_diffs[interval]) + 1,
        _start_time - interval_diff,
        _start_time,
        _end_time,
        _end_time + interval_diff,
    )


def get_trunc_func(period: str) -> str:
    if period == "hour":
        return "hour"
    elif period == "day":
        return "day"
    elif period == "week":
        return "week"
    elif period == "month":
        return "month"
    else:
        raise ValueError(f"Period {period} is unsupported.")


class LifecycleTrend:
    def _serialize_lifecycle(self, entity: Entity, filter: Filter, team_id: int) -> List[Dict[str, Any]]:

        period = filter.interval or "day"
        num_intervals, prev_date_from, date_from, date_to, after_date_to = get_time_diff(
            period, filter.date_from, filter.date_to, team_id
        )
        interval_trunc = get_trunc_func(period=period)

        with connection.cursor() as cursor:
            cursor.execute(
                LIFECYCLE_SQL.format(
                    action_join=ACTION_JOIN if entity.type == TREND_FILTER_TYPE_ACTIONS else "",
                    event_condition="{} = %(event)s".format(
                        "action_id" if entity.type == TREND_FILTER_TYPE_ACTIONS else "event"
                    ),
                ),
                {
                    "team_id": team_id,
                    "event": entity.id,
                    "interval": interval_trunc,
                    "one_interval": "1 " + interval_trunc,
                    "num_intervals": num_intervals,
                    "prev_date_from": prev_date_from,
                    "date_from": date_from,
                    "date_to": date_to,
                    "after_date_to": after_date_to,
                },
            )
            res = []
            for val in cursor.fetchall():
                label = "{} - {}".format(entity.name, val[2])
                additional_values = {"label": label, "status": val[2]}
                parsed_result = parse_response(val, filter, additional_values)
                res.append(parsed_result)

        return res


def parse_response(stats: Dict, filter: Filter, additional_values: Dict = {}) -> Dict[str, Any]:
    counts = stats[1]
    dates = [
        ((item - timedelta(days=1)) if filter.interval == "month" else item).strftime(
            "%Y-%m-%d{}".format(", %H:%M" if filter.interval == "hour" or filter.interval == "minute" else "")
        )
        for item in stats[0]
    ]
    labels = [
        ((item - timedelta(days=1)) if filter.interval == "month" else item).strftime(
            "%a. %-d %B{}".format(", %H:%M" if filter.interval == "hour" or filter.interval == "minute" else "")
        )
        for item in stats[0]
    ]
    days = [
        ((item - timedelta(days=1)) if filter.interval == "month" else item).strftime(
            "%Y-%m-%d{}".format(" %H:%M:%S" if filter.interval == "hour" or filter.interval == "minute" else "")
        )
        for item in stats[0]
    ]
    return {
        "data": counts,
        "count": sum(counts),
        "dates": dates,
        "labels": labels,
        "days": days,
        **additional_values,
    }
