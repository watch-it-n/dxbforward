import asyncio
import logging
import os
import argparse
import re
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import PeerUser, PeerChat, PeerChannel

# Load environment variables
load_dotenv()


def add_infinity_to_video_caption(caption):
    """Replace the filename/tag component before MP4/MKV and drop trailing text."""
    if not caption:
        return caption

    match = re.search(
        r"(?P<prefix>.*[ \t._-])?[^ \t._-]+(?P<extension>\.(?:mp4|mkv))",
        caption,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return caption

    return f"{match.group('prefix') or ''}Infinity{match.group('extension')}"


def setup_logging(disable_console=False):
    """Configure logging based on console preference."""
    if disable_console:
        # Only log to file, not console
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('telegram_forwarder.log'),
            ]
        )
    else:
        # Log to both console and file
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler('telegram_forwarder.log')
            ]
        )

logger = logging.getLogger(__name__)

class TelegramForwarder:
    def __init__(self, remove_forward_signature=False):
        """Initialize the Telegram forwarder with environment variables."""
        self.api_id = os.getenv('API_ID')
        self.api_hash = os.getenv('API_HASH')
        self.bot_token = os.getenv('BOT_TOKEN')
        configured_bot_mode = os.getenv('BOT_MODE', '').strip().lower() in {'1', 'true', 'yes', 'on'}
        self.is_bot_mode = bool(self.bot_token) or configured_bot_mode
        configured_remove_signature = os.getenv('REMOVE_FORWARD_SIGNATURE', '').strip().lower() in {
            '1', 'true', 'yes', 'on'
        }
        self.remove_forward_signature = remove_forward_signature or configured_remove_signature
        
        # Check for legacy single source/target configuration
        self.source_id = os.getenv('SOURCE_ID')
        self.target_id = os.getenv('TARGET_ID')
        self.forwarding_rules = os.getenv('FORWARDING_RULES')
        
        # Validate required environment variables
        if not all([self.api_id, self.api_hash]):
            raise ValueError("Missing API_ID or API_HASH. Check your .env file.")
        
        # Parse forwarding configuration
        self.forwarding_map = self._parse_forwarding_rules()
        self.ready = False
        self.health_server = None
        
        if not self.forwarding_map:
            raise ValueError("No forwarding rules configured. Set either SOURCE_ID/TARGET_ID or FORWARDING_RULES.")
        
        # Initialize Telegram client
        if self.is_bot_mode:
            # Bot mode
            self.client = TelegramClient('bot_session', self.api_id, self.api_hash)
            logger.info("Initialized in bot mode")
        else:
            # User mode
            self.client = TelegramClient('user_session', self.api_id, self.api_hash)
            logger.info("Initialized in user mode")
    
    def _parse_forwarding_rules(self):
        """Parse forwarding rules from environment variables."""
        forwarding_map = {}
        
        # Check for legacy single source/target configuration
        if self.source_id and self.target_id:
            try:
                source_id = int(self.source_id)
                target_id = int(self.target_id)
                forwarding_map[source_id] = [target_id]
                logger.info("Using legacy single source/target configuration")
                return forwarding_map
            except ValueError:
                raise ValueError("SOURCE_ID and TARGET_ID must be valid integers.")
        
        # Parse new multiple forwarding rules
        if self.forwarding_rules:
            try:
                # Format: source1:target1:target2,source2:target3,source3:target4
                rules = self.forwarding_rules.split(',')
                for rule in rules:
                    rule = rule.strip()
                    if not rule:
                        continue
                    
                    parts = rule.split(':')
                    if len(parts) < 2:
                        raise ValueError(f"Invalid forwarding rule format: {rule}")
                    
                    source_id = int(parts[0])
                    target_ids = [int(target) for target in parts[1:]]
                    
                    if source_id in forwarding_map:
                        # Extend existing targets for this source
                        forwarding_map[source_id].extend(target_ids)
                    else:
                        forwarding_map[source_id] = target_ids
                
                logger.info(f"Parsed {len(forwarding_map)} forwarding rules")
                return forwarding_map
                
            except ValueError as e:
                raise ValueError(f"Error parsing FORWARDING_RULES: {e}")
        
        return {}
    
    async def start_client(self):
        """Start the Telegram client and handle authentication."""
        await self.client.start(bot_token=self.bot_token if self.bot_token else None)
        
        if not self.is_bot_mode:
            # User authentication
            if not await self.client.is_user_authorized():
                phone = input("Enter your phone number: ")
                await self.client.send_code_request(phone)
                code = input("Enter the code you received: ")
                
                try:
                    await self.client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    password = input("Enter your 2FA password: ")
                    await self.client.sign_in(password=password)
        
        logger.info("Client started successfully")
    
    async def get_entity_info(self, entity_id):
        """Get information about an entity (user, chat, or channel)."""
        try:
            entity = await self.client.get_entity(entity_id)
            if hasattr(entity, 'title'):
                return f"{entity.title} (ID: {entity_id})"
            elif hasattr(entity, 'first_name'):
                name = entity.first_name
                if hasattr(entity, 'last_name') and entity.last_name:
                    name += f" {entity.last_name}"
                return f"{name} (ID: {entity_id})"
            else:
                return f"Entity (ID: {entity_id})"
        except Exception as e:
            logger.error(f"Error getting entity info for {entity_id}: {e}")
            return f"Unknown Entity (ID: {entity_id})"
    
    async def setup_forwarding(self):
        """Set up message forwarding from multiple sources to their respective targets."""
        # A bare numeric channel ID does not include Telegram's access hash.
        # User sessions can obtain it by loading their dialogs; bots cannot use
        # GetDialogs, so leave bot resolution to entities delivered in updates.
        if not self.is_bot_mode:
            logger.info("Loading account dialogs to resolve private channel IDs...")
            await self.client.get_dialogs(limit=None)

        # Log all forwarding rules
        logger.info("Setting up forwarding rules:")
        for source_id, target_ids in self.forwarding_map.items():
            source_info = await self.get_entity_info(source_id)
            target_infos = []
            for target_id in target_ids:
                target_info = await self.get_entity_info(target_id)
                target_infos.append(target_info)
            logger.info(f"  {source_info} -> {', '.join(target_infos)}")
        
        # Get all source IDs for the event handler
        source_ids = list(self.forwarding_map.keys())
        
        @self.client.on(events.NewMessage(chats=source_ids))
        async def forward_handler(event):
            """Handle new messages and forward them to configured targets."""
            try:
                # Get message details
                message = event.message
                source_id = event.chat_id
                sender_id = message.sender_id if message.sender_id else "Unknown"
                
                # Get target IDs for this source
                target_ids = self.forwarding_map.get(source_id, [])
                if not target_ids:
                    logger.warning(f"No targets configured for source {source_id}")
                    return
                
                source_info = await self.get_entity_info(source_id)
                logger.info(f"Received message from {sender_id} in {source_info}")
                
                # Forward to all configured targets
                for target_id in target_ids:
                    try:
                        target_info = await self.get_entity_info(target_id)

                        caption = message.message
                        updated_caption = add_infinity_to_video_caption(caption)
                        caption_updated = bool(
                            message.media and caption and updated_caption != caption
                        )

                        if self.remove_forward_signature or caption_updated:
                            # Send as new message without "Forward from..." signature
                            await self.client.send_message(
                                entity=target_id,
                                message=updated_caption,
                                file=message.media,
                                parse_mode='html' if message.entities else None
                            )
                            if caption_updated and not self.remove_forward_signature:
                                logger.info(
                                    f"Successfully sent video with updated caption (without forward attribution) to {target_info}"
                                )
                            else:
                                logger.info(f"Successfully sent message (without forward signature) to {target_info}")
                        else:
                            # Forward the message with "Forward from..." signature
                            await self.client.forward_messages(
                                entity=target_id,
                                messages=message.id,
                                from_peer=source_id
                            )
                            logger.info(f"Successfully forwarded message to {target_info}")
                            
                    except Exception as e:
                        target_info = await self.get_entity_info(target_id)
                        logger.error(f"Error forwarding to {target_info}: {e}")
                
            except FloodWaitError as e:
                logger.warning(f"Rate limited. Waiting {e.seconds} seconds...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Error in forward handler: {e}")
        
        logger.info("Message forwarding handlers registered successfully")

        if self.is_bot_mode:
            @self.client.on(events.NewMessage(pattern=r'^/start(?:@\w+)?(?:\s|$)'))
            async def start_handler(event):
                """Confirm the bot is online and its forwarding listener is active."""
                if not event.is_private:
                    return
                await event.respond(
                    f"I'm online and monitoring {len(self.forwarding_map)} source channel(s)."
                )
                logger.info("Answered /start from user %s", event.sender_id)
    
    async def run(self):
        """Main method to run the forwarder."""
        try:
            port = os.getenv('PORT')
            if port:
                self.health_server = await asyncio.start_server(
                    self._handle_health_request,
                    host='0.0.0.0',
                    port=int(port),
                )
                logger.info("Health endpoint listening on 0.0.0.0:%s/healthz", port)

            await self.start_client()
            await self.setup_forwarding()
            self.ready = True
            
            logger.info("Telegram forwarder is now running. Press Ctrl+C to stop.")
            await self.client.run_until_disconnected()
            
        except KeyboardInterrupt:
            logger.info("Received interrupt signal. Stopping...")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            self.ready = False
            if self.health_server:
                self.health_server.close()
                await self.health_server.wait_closed()
            await self.client.disconnect()
            logger.info("Client disconnected")

    async def _handle_health_request(self, reader, writer):
        """Serve a small HTTP health check for Render and uptime monitors."""
        status = '404 Not Found'
        body = 'not found'
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode('latin-1').strip().split()
            if len(parts) >= 2 and parts[0] == 'GET':
                path = parts[1].split('?', 1)[0]
                if path == '/healthz':
                    if self.ready and self.client.is_connected():
                        status, body = '200 OK', 'ok'
                    else:
                        status, body = '503 Service Unavailable', 'starting'
            elif parts:
                status, body = '405 Method Not Allowed', 'method not allowed'
        except (asyncio.TimeoutError, UnicodeDecodeError):
            status, body = '400 Bad Request', 'bad request'

        payload = body.encode('utf-8')
        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode('ascii') + payload
        writer.write(response)
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

async def main():
    """Main function to run the application."""
    parser = argparse.ArgumentParser(description='Telegram Message Forwarder')
    parser.add_argument('--remove-forward-signature', '-r', action='store_true',
                        help='Remove "Forward from..." signature by sending as new messages instead of forwarding')
    parser.add_argument('--disable-console-log', '-q', action='store_true',
                        help='Disable console logging (only log to file)')
    
    args = parser.parse_args()
    
    # Setup logging based on arguments
    setup_logging(disable_console=args.disable_console_log)
    
    try:
        forwarder = TelegramForwarder(remove_forward_signature=args.remove_forward_signature)
        await forwarder.run()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        print("\nPlease check your .env file and ensure all required variables are set.")
        print("You can use .env.example as a template.")
    except Exception as e:
        logger.error(f"Application error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
