import asyncio
from time import time

import aiofiles.os
from aiogram import Bot, flags
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from nazurin import config
from nazurin.database import Database
from nazurin.models import File, Illust, Ugoira
from nazurin.sites import SiteManager
from nazurin.storage import Storage
from nazurin.utils import logger
from nazurin.utils.decorators import retry_after
from nazurin.utils.exceptions import AlreadyExistsError, NazurinError
from nazurin.utils.helpers import (
    handle_bad_request,
    remove_files_older_than,
    resize_image_for_telegram,
    sanitize_caption,
)

TG_GROUP_NUMBER_LIMIT = 10
TG_GROUP_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MB


class NazurinBot(Bot):
    send_message = retry_after(Bot.send_message)

    def __init__(self, *args, **kwargs):
        session = AiohttpSession(proxy=config.PROXY) if config.PROXY else None
        super().__init__(
            *args,
            token=config.TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=session,
            **kwargs,
        )
        self.sites = SiteManager()
        self.storage = Storage()
        self.cleanup_task = None

    def init(self):
        self.sites.load()
        self.storage.load()

    async def on_startup(self):
        self.cleanup_task = asyncio.create_task(self.cleanup_temp_dir())

    async def on_shutdown(self):
        if self.cleanup_task:
            self.cleanup_task.cancel()

    @retry_after
    @flags.chat_action(ChatAction.UPLOAD_PHOTO)
    async def send_single_group(
        self,
        imgs: list[InputMediaPhoto],
        caption: str,
        chat_id: int,
        reply_to: int | None = None,
    ):
        imgs[0].caption = caption
        await self.send_media_group(chat_id, imgs, reply_to_message_id=reply_to)

    async def send_photos(
        self,
        illust: Illust,
        chat_id: int,
        reply_to: int | None = None,
    ):
        caption = sanitize_caption(illust.caption)
        groups = []
        imgs = illust.images
        if len(imgs) == 0:
            raise NazurinError("No image to send, try download option.")

        # Split a list of images into groups of max 50MB each
        groups: list[list[InputMediaPhoto]] = []
        current_group: list[InputMediaPhoto] = []
        current_size = 0

        for img in imgs:
            resized_img = await resize_image_for_telegram(img.path)
            img_size = await aiofiles.os.path.getsize(resized_img)
            if (
                len(current_group) >= TG_GROUP_NUMBER_LIMIT
                or current_size + img_size > TG_GROUP_SIZE_LIMIT
            ):
                groups.append(current_group)
                current_group = []
                current_size = 0
            current_group.append(InputMediaPhoto(media=FSInputFile(resized_img)))
            current_size += img_size

        if current_group:
            groups.append(current_group)

        for group in groups:
            await self.send_single_group(group, caption, chat_id, reply_to)

    async def send_illust(
        self,
        illust: Illust,
        message: Message | None = None,
        chat_id: int | None = None,
    ):
        reply_to = message.message_id if message else None
        if not chat_id:
            chat_id = message.chat.id
        elif chat_id != reply_to:  # Sending to different chat, can't reply
            reply_to = None
        try:
            if isinstance(illust, Ugoira):
                # Note: Videos have a 50MB limit, but we only resize images for now
                await self.send_animation(
                    chat_id,
                    FSInputFile(illust.video.path),  # TODO: Handle URL
                    caption=sanitize_caption(illust.caption),
                    reply_to_message_id=reply_to,
                )
            else:
                await illust.download()
                await self.send_photos(illust, chat_id, reply_to)
        except TelegramBadRequest as error:
            await handle_bad_request(message, error)

    @retry_after
    @flags.chat_action(ChatAction.UPLOAD_DOCUMENT)
    async def send_doc(self, file: File, chat_id, message_id=None):
        await self.send_document(
            chat_id,
            FSInputFile(file.path),
            reply_to_message_id=message_id,
        )

    async def send_docs(
        self,
        illust: Illust,
        message: Message | None = None,
        chat_id=None,
    ):
        if message:
            message_id = message.message_id
            if not chat_id:
                chat_id = message.chat.id
        else:
            message_id = None  # Sending to channel, no message to reply
        for file in illust.all_files:
            await self.send_doc(file, chat_id, message_id)

    async def send_to_gallery(
        self,
        urls: list[str],
        illust: Illust,
        message: Message | None = None,
    ):
        if isinstance(illust, Ugoira):
            await self.send_illust(illust, message, config.GALLERY_ID)
        # Forward disabled
        #
        # elif (
        #     message
        #     and message.forward_origin is not None
        #     and message.photo
        #     # If there're multiple images,
        #     # then send a new message instead of forwarding an existing one,
        #     # since we currently can't forward albums correctly.
        #     and not illust.has_multiple_images()
        # ):
        #     await message.forward(config.GALLERY_ID)
        elif not illust.has_image():
            await self.send_message(config.GALLERY_ID, "\n".join(urls))
        else:
            await self.send_illust(illust, message, config.GALLERY_ID)

    async def update_collection(
        self,
        urls: list[str],
        message: Message | None = None,
    ):
        result = self.sites.match(urls)
        if not result:
            raise NazurinError("No source matched")
        logger.info(
            "Collection update: source={}, match={}",
            result.source.name,
            result.match.groups(),
        )

        illust, document = await self.sites.handle_update(result)

        db = Database().driver()
        collection = db.collection(document.collection)
        if await collection.document(document.id).exists():
            raise AlreadyExistsError

        # Send / Forward to gallery & Save to album
        await illust.download()
        if config.GALLERY_ID:
            await self.send_to_gallery(urls, illust, message)

        await self.storage.store(illust)
        document.data["collected_at"] = time()
        await collection.insert(document.id, document.data)
        return True

    async def cleanup_temp_dir(self):
        if config.CLEANUP_INTERVAL == 0:
            return
        while True:
            logger.info("Cleaning up temporary directory")
            try:
                await remove_files_older_than(config.TEMP_DIR, 1)
                logger.info("Cleaned up temporary directory")
            # pylint: disable=broad-except
            except Exception as error:
                logger.error("Failed to clean up temporary directory: {}", error)
            await asyncio.sleep(config.CLEANUP_INTERVAL * 86400)
