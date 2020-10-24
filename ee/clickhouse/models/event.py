import json
import uuid
from typing import Dict, List, Optional, Tuple, Union

import pytz
from dateutil.parser import isoparse
from django.utils import timezone
from rest_framework import serializers

from ee.clickhouse.client import sync_execute
from ee.clickhouse.models.element import chain_to_elements, elements_to_string
from ee.clickhouse.sql.events import GET_EVENTS_BY_TEAM_SQL, GET_EVENTS_SQL, INSERT_EVENT_SQL
from ee.dynamodb.models.events import Event as DynamoEvent
from ee.kafka_client.client import ClickhouseProducer
from ee.kafka_client.topics import KAFKA_EVENTS
from posthog.models.element import Element
from posthog.models.person import Person
from posthog.models.team import Team


def create_event(
    event_uuid: uuid.UUID,
    event: str,
    team: Team,
    distinct_id: str,
    timestamp: Optional[Union[timezone.datetime, str]] = None,
    properties: Optional[Dict] = {},
    elements: Optional[List[Element]] = None,
    person_uuid: str = None,
) -> str:

    if not timestamp:
        timestamp = timezone.now()
    assert timestamp is not None

    # clickhouse specific formatting
    if isinstance(timestamp, str):
        timestamp = isoparse(timestamp)
    else:
        timestamp = timestamp.astimezone(pytz.utc)

    elements_chain = ""
    if elements and len(elements) > 0:
        elements_chain = elements_to_string(elements=elements)

    if not person_uuid:
        try:
            person = Person.objects.get(persondistinctid__distinct_id=distinct_id)
            person_uuid = str(person.uuid)
        except Person.DoesNotExist:
            person_uuid = None

    data = {
        "uuid": str(event_uuid),
        "event": event,
        "properties": json.dumps(properties),
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "team_id": team.pk,
        "distinct_id": distinct_id,
        "created_at": timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "elements_chain": elements_chain,
        "person_uuid": person_uuid if person_uuid else "",
        "sign": 1,
    }
    p = ClickhouseProducer()
    p.produce(sql=INSERT_EVENT_SQL, topic=KAFKA_EVENTS, data=data)

    de = DynamoEvent(
        distinct_id=distinct_id,
        uuid=str(event_uuid),
        event=event,
        properties=properties,
        timestamp=timestamp,
        team_id=team.pk,
        created_at=timestamp,
        elements_chain=elements_chain,
        person_uuid=person_uuid,
    )
    de.save()
    return str(event_uuid)


def delete_event(event: DynamoEvent) -> None:
    data = {
        "uuid": event.uuid,
        "event": event.event,
        "properties": json.dumps(event.properties),
        "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "team_id": event.team_id,
        "distinct_id": event.distinct_id,
        "created_at": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "elements_chain": event.elements_chain if event.elements_chain else "",
        "person_uuid": event.person_uuid if event.person_uuid else "",
        "sign": -1,
    }

    p = ClickhouseProducer()
    p.produce(sql=INSERT_EVENT_SQL, topic=KAFKA_EVENTS, data=data)
    return


def update_event(event: DynamoEvent):
    data = {
        "uuid": event.uuid,
        "event": event.event,
        "properties": json.dumps(event.properties),
        "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "team_id": event.team_id,
        "distinct_id": event.distinct_id,
        "created_at": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "elements_chain": event.elements_chain if event.elements_chain else "",
        "person_uuid": event.person_uuid,
        "sign": 1,
    }
    p = ClickhouseProducer()
    p.produce(sql=INSERT_EVENT_SQL, topic=KAFKA_EVENTS, data=data)
    return


def get_events():
    events = sync_execute(GET_EVENTS_SQL)
    return ClickhouseEventSerializer(events, many=True, context={"elements": None, "people": None}).data


def get_events_by_team(team_id: Union[str, int]):
    events = sync_execute(GET_EVENTS_BY_TEAM_SQL, {"team_id": str(team_id)})
    return ClickhouseEventSerializer(events, many=True, context={"elements": None, "people": None}).data


class ElementSerializer(serializers.ModelSerializer):
    event = serializers.CharField()

    class Meta:
        model = Element
        fields = [
            "event",
            "text",
            "tag_name",
            "attr_class",
            "href",
            "attr_id",
            "nth_child",
            "nth_of_type",
            "attributes",
            "order",
        ]


# reference raw sql for
class ClickhouseEventSerializer(serializers.Serializer):
    id = serializers.SerializerMethodField()
    distinct_id = serializers.SerializerMethodField()
    properties = serializers.SerializerMethodField()
    event = serializers.SerializerMethodField()
    timestamp = serializers.SerializerMethodField()
    person = serializers.SerializerMethodField()
    elements = serializers.SerializerMethodField()
    elements_chain = serializers.SerializerMethodField()
    person_uuid = serializers.SerializerMethodField()

    def get_id(self, event):
        return str(event[0])

    def get_distinct_id(self, event):
        return event[5]

    def get_properties(self, event):
        if len(event) >= 10 and event[9] and event[10]:
            prop_vals = [res.strip('"') for res in event[10]]
            return dict(zip(event[9], prop_vals))
        else:
            props = json.loads(event[2])
            unpadded = {key: value.strip('"') if isinstance(value, str) else value for key, value in props.items()}
            return unpadded

    def get_event(self, event):
        return event[1]

    def get_timestamp(self, event):
        dt = event[3].replace(tzinfo=timezone.utc)
        return dt.astimezone().isoformat()

    def get_person(self, event):
        if not self.context.get("people") or event[5] not in self.context["people"]:
            return event[5]
        return self.context["people"][event[5]].properties.get("email", event[5])

    def get_elements(self, event):
        if not event[6]:
            return []
        return ElementSerializer(chain_to_elements(event[6]), many=True).data

    def get_elements_chain(self, event):
        return event[6]

    def get_person_uuid(self, event):
        return event[8]


def determine_event_conditions(conditions: Dict[str, Union[str, List[str]]]) -> Tuple[str, Dict]:
    result = ""
    params: Dict[str, Union[str, List[str]]] = {}
    for idx, (k, v) in enumerate(conditions.items()):
        if not isinstance(v, str):
            continue
        if k == "after":
            timestamp = isoparse(v).strftime("%Y-%m-%d %H:%M:%S.%f")
            result += "AND timestamp > %(after)s"
            params.update({"after": timestamp})
        elif k == "before":
            timestamp = isoparse(v).strftime("%Y-%m-%d %H:%M:%S.%f")
            result += "AND timestamp < %(before)s"
            params.update({"before": timestamp})
        elif k == "person_id":
            result += """AND distinct_id IN (%(distinct_ids)s)"""
            distinct_ids = Person.objects.filter(pk=v)[0].distinct_ids
            distinct_ids = [distinct_id.__str__() for distinct_id in distinct_ids]
            params.update({"distinct_ids": distinct_ids})
        elif k == "distinct_id":
            result += "AND distinct_id = %(distinct_id)s"
            params.update({"distinct_id": v})
        elif k == "event":
            result += "AND event = %(event)s"
            params.update({"event": v})
    return result, params
