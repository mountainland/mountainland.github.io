import logging

# noinspection PyPackageRequirements
from nio import JoinError, MatrixRoom, Event

from middleman.bot_commands import Command
from middleman.chat_functions import send_text_to_room
from middleman.message_responses import Message
from middleman.utils import with_ratelimit

logger = logging.getLogger(__name__)

DUPLICATES_CACHE_SIZE = 1000


class Callbacks(object):
    def __init__(self, client, store, config):
        """
        Args:
            client (nio.AsyncClient): nio client used to interact with matrix

            store (Storage): Bot storage

            config (Config): Bot configuration parameters
        """
        self.client = client
        self.store = store
        self.config = config
        self.command_prefix = config.command_prefix
        self.received_events = []
        self.welcome_message_sent_to_room = []

    async def decryption_failure(self, room: MatrixRoom, event: Event):
        """Callback for when an event fails to decrypt."""
        message = f"Failed to decrypt event {event.event_id} in room {room.name} ({room.canonical_alias} / " \
                  f"{room.room_id}) from sender {event.sender}."
        logger.error(message)

        await send_text_to_room(
            client=self.client,
            room=self.config.management_room_id,
            message=message,
            notice=True,
        )

    def trim_duplicates_caches(self):
        if len(self.received_events) > DUPLICATES_CACHE_SIZE:
            self.received_events = self.received_events[:DUPLICATES_CACHE_SIZE]
        if len(self.welcome_message_sent_to_room) > DUPLICATES_CACHE_SIZE:
            self.welcome_message_sent_to_room = self.welcome_message_sent_to_room[:DUPLICATES_CACHE_SIZE]

    async def member(self, room, event):
        """Callback for when a room member event is received.

        Args:
            room (nio.rooms.MatrixRoom): The room the event came from

            event (nio.events.room_events.RoomMemberEvent): The event
        """
        self.trim_duplicates_caches()
        if self.should_process(event.event_id) is False:
            return
        logger.debug(
            f"Received a room member event for {room.display_name} | "
            f"{event.sender}: {event.membership}"
        )

        # Ignore if it was not us joining the room
        if event.sender != self.client.user:
            return

        # Ignore if we didn't join
        if event.membership != "join" or event.prev_content.get("membership") == "join":
            return

        # Send welcome message if configured
        if self.config.welcome_message:
            if room.room_id in self.welcome_message_sent_to_room:
                logger.debug(f"Not sending welcome message to room {room.room_id} - it's been sent already!")
                return
            # Send welcome message
            logger.info(f"Sending welcome message to room {room.room_id}")
            self.welcome_message_sent_to_room.insert(0, room.room_id)
            await send_text_to_room(self.client, room.room_id, self.config.welcome_message, True)

        # Notify the management room for visibility
        logger.info(f"Notifying management room of room join to {room.room_id}")
        await send_text_to_room(
            self.client,
            self.config.management_room_id,
            f"I have joined room {room.display_name} (`{room.room_id}`) after being invited.",
            True,
        )

    async def message(self, room, event):
        """Callback for when a message event is received

        Args:
            room (nio.rooms.MatrixRoom): The room the event came from

            event (nio.events.room_events.RoomMessageText): The event defining the message

        """
        self.trim_duplicates_caches()
        if self.should_process(event.event_id) is False:
            return
        # Extract the message text
        msg = event.body

        # Ignore messages from ourselves
        if event.sender == self.client.user:
            return

        # If this looks like an edit, strip the edit prefix
        if msg.startswith(" * "):
            msg = msg[3:]

        logger.debug(
            f"Bot message received for room {room.display_name} | "
            f"{room.user_name(event.sender)} (named: {room.is_named}, name: {room.name}, "
            f"alias: {room.canonical_alias}): {msg}"
        )

        # Process as message if in a public room without command prefix
        has_command_prefix = msg.startswith(self.command_prefix) or msg.startswith("!message")

        if has_command_prefix:
            if msg.startswith("!message"):
                msg = msg[1:]
            else:
                # Remove the command prefix
                msg = msg[len(self.command_prefix):]

            command = Command(self.client, self.store, self.config, msg, room, event)
            await command.process()
        else:
            # General message listener
            message = Message(self.client, self.store, self.config, msg, room, event)
            await message.process()

    async def invite(self, room, event):
        """Callback for when an invite is received. Join the room specified in the invite"""
        if self.should_process(event.source.get("event_id")) is False:
            return
        logger.debug(f"Got invite to {room.room_id} from {event.sender}.")

        result = await with_ratelimit(self.client, "join", room.room_id)
        if type(result) == JoinError:
            logger.error("Unable to join room: %s", room.room_id)
            return

        logger.info(f"Joined {room.room_id}")

    def should_process(self, event_id: str) -> bool:
        logger.debug(f"Callback received event: {event_id}")
        if event_id in self.received_events:
            logger.debug(f"Skipping {event_id} as it's already processed")
            return False
        self.received_events.insert(0, event_id)
        return True
