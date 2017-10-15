"""
Telematrix

App service for Matrix to bridge a room with a Telegram group.
"""
import asyncio
import html
import json
import logging
import mimetypes
import sys
from datetime import datetime
from time import time
from urllib.parse import unquote, quote, urlparse, parse_qs
from io import BytesIO

from PIL import Image
from aiohttp import web, ClientSession
from aiotg import Bot
from bs4 import BeautifulSoup

import telematrix.database as db

# Read the configuration file
try:
    with open('config.json', 'r') as config_file:
        CONFIG = json.load(config_file)

        HS_TOKEN = CONFIG['tokens']['hs']
        AS_TOKEN = CONFIG['tokens']['as']
        TG_TOKEN = CONFIG['tokens']['telegram']

        try:
            GOOGLE_TOKEN = CONFIG['tokens']['google']
        except KeyError:
            GOOGLE_TOKEN = None

        MATRIX_HOST = CONFIG['hosts']['internal']
        MATRIX_HOST_EXT = CONFIG['hosts']['external']
        MATRIX_HOST_BARE = CONFIG['hosts']['bare']

        MATRIX_PREFIX = MATRIX_HOST + '_matrix/client/r0/'
        MATRIX_MEDIA_PREFIX = MATRIX_HOST + '_matrix/media/r0/'

        USER_ID_FORMAT = CONFIG['user_id_format']
        DATABASE_URL = CONFIG['db_url']

except (OSError, IOError) as exception:
    print('Error opening config file:')
    print(exception)
    exit(1)

GOO_GL_URL = 'https://www.googleapis.com/urlshortener/v1/url'

TG_BOT = Bot(api_token=TG_TOKEN)
MATRIX_SESS = ClientSession()
SHORTEN_SESS = ClientSession()


def create_response(code, obj):
    """
    Create an HTTP response with a JSON body.
    :param code: The status code of the response.
    :param obj: The object to serialize and include in the response.
    :return: A web.Response.
    """
    return web.Response(text=json.dumps(obj), status=code,
                        content_type='application/json', charset='utf-8')


VALID_TAGS = ['b', 'strong', 'i', 'em', 'a', 'pre']


def sanitize_html(string):
    """
    Sanitize an HTML string for the Telegram bot API.
    :param string: The HTML string to sanitized.
    :return: The sanitized HTML string.
    """
    string = string.replace('<br>', '\n').replace('<br/>', '\n') \
                   .replace('<br />', '\n')
    soup = BeautifulSoup(string, 'html.parser')
    for tag in soup.find_all(True):
        if tag.name == 'blockquote':
            tag.string = ('\n' + tag.text).replace('\n', '\n> ').rstrip('\n>')
        if tag.name not in VALID_TAGS:
            tag.hidden = True
    return soup.renderContents().decode('utf-8')


def format_matrix_msg(form, username, content):
    """
    Formats a matrix message for sending to Telegram
    :param form: The format string of the message, where the first parameter
                 is the username and the second one the message.
    :param username: The username of the user.
    :param content: The content to be sent.
    :return: The formatted string.
    """
    if 'format' in content and content['format'] == 'org.matrix.custom.html':
        sanitized = sanitize_html(content['formatted_body'])
        return html.escape(form).format(username, sanitized), 'HTML'
    else:
        return form.format(username, content['body']), None


async def download_matrix_file(url, filename):
    """
    Download a file from an MXC URL to /tmp/{filename}
    :param url: The MXC URL to download from.
    :param filename: The filename in /tmp/ to download into.
    """
    m_url = MATRIX_MEDIA_PREFIX + 'download/{}{}'.format(url.netloc, url.path)
    async with MATRIX_SESS.get(m_url) as response:
        data = await response.read()
    with open('/tmp/{}'.format(filename), 'wb') as file:
        file.write(data)


async def shorten_url(url):
    """
    Shorten an URL using goo.gl. Returns the original URL if it fails.
    :param url: The URL to shorten.
    :return: The shortened URL.
    """
    if not GOOGLE_TOKEN:
        return url

    headers = {'Content-Type': 'application/json'}
    async with SHORTEN_SESS.post(GOO_GL_URL, params={'key': GOOGLE_TOKEN},
                                 data=json.dumps({'longUrl': url}),
                                 headers=headers) \
            as response:
        obj = await response.json()

    if 'id' in obj:
        return obj['id']
    else:
        return url


def matrix_is_telegram(user_id):
    """
    Checks if a Matrix user_id is a Telegram user

    :param user_id: The Matrix user_id to check.
    :return: True if the user is a Telegram user
    """
    username = user_id.split(':')[0][1:]
    return username.startswith('telegram_')


def get_username(user_id):
    """
    Gets the username of a Matrix user_id

    :param user_id: The Matrix user_id to extract the username from.
    :return: The username
    """
    return user_id.split(':')[0][1:]


mime_extensions = {
    'image/jpeg': 'jpg',
    'image/gif': 'gif',
    'image/png': 'png',
    'image/tiff': 'tif',
    'image/x-tiff': 'tif',
    'image/bmp': 'bmp',
    'image/x-windows-bmp': 'bmp'
}

async def _matrix_on_aliases(event, link, _):
    """Handles an m.room.aliases event from the Matrix homeserver."""

    # Discard events that are not from the linked server
    if event['state_key'] != MATRIX_HOST_BARE:
        return

    # Delete all old links for the room
    links = db.session.query(db.ChatLink)\
              .filter_by(matrix_room=event['room_id']).all()
    for link in links:
        db.session.delete(link)

    # Add all relevant aliases to the database
    aliases = event['content']['aliases']
    for alias in aliases:
        if alias.split('_')[0] != '#telegram' or alias.split(':')[-1] != MATRIX_HOST_BARE:
            continue

        telegram_id = alias.split('_')[1].split(':')[0]
        link = db.ChatLink(event['room_id'], telegram_id, True)
        db.session.add(link)

    db.session.commit()

async def _matrix_on_message_image(displayname, group, content):
    """
    Handles an m.image in an m.room.message event from the Matrix homeserver.
    """

    url = urlparse(content['url'])

    # Append the correct extension if it's missing or wrong
    extension = mime_extensions[content['info']['mimetype']]
    if not content['body'].endswith(extension):
        content['body'] += '.' + extension

    # Download the file
    await download_matrix_file(url, content['body'])
    with open('/tmp/{}'.format(content['body']), 'rb') as img_file:
        # Create the URL and shorten it
        url_str = '{}_matrix/media/r0/download/{}{}' \
                  .format(MATRIX_HOST_EXT, url.netloc, quote(url.path))
        url_str = await shorten_url(url_str)

        caption = '<{}> {} ({})'.format(displayname, content['body'], url_str)
        return await group.send_photo(img_file, caption=caption)


async def _matrix_on_message(event, _, group):
    """Handles an m.room.message event from the Matrix homeserver."""

    user_id = event['user_id']
    content = event['content']

    # Ignore own or unsupported messages
    if matrix_is_telegram(user_id) or 'msgtype' not in content:
        return

    sender = db.session.query(db.MatrixUser).filter_by(matrix_id=user_id).first()
    if not sender:
        response = await matrix_get('client', 'profile/{}/displayname'.format(user_id), None)
        try:
            displayname = response['displayname']
        except KeyError:
            displayname = get_username(user_id)

        sender = db.MatrixUser(user_id, displayname)
        db.session.add(sender)
        db.session.commit()
    else:
        displayname = sender.name or get_username(user_id)


    if content['msgtype'] == 'm.text':
        msg, mode = format_matrix_msg('<{}> {}', displayname, content)
        response = await group.send_text(msg, parse_mode=mode)
    elif content['msgtype'] == 'm.notice':
        msg, mode = format_matrix_msg('[{}] {}', displayname, content)
        response = await group.send_text(msg, parse_mode=mode)
    elif content['msgtype'] == 'm.emote':
        msg, mode = format_matrix_msg('* {} {}', displayname, content)
        response = await group.send_text(msg, parse_mode=mode)
    elif content['msgtype'] == 'm.image':
        try:
            response = await _matrix_on_message_image(displayname, group, content)
        except Exception as e:
            print(f'Error bridging image from Matrix to Telegram: {e}', file=sys.stderr)
            response = None
    else:
        print(f'Unsupported message type {content["msgtype"]}', file=sys.stderr)
        response = None

    if response:
        message = db.Message(response['result']['chat']['id'], response['result']['message_id'],
                             event['room_id'], event['event_id'], displayname)
        db.session.add(message)
        db.session.commit()

    return response


async def _matrix_on_member(event, _, group):
    """Handles an m.room.member event from the Matrix homeserver."""

    # Ignore own events
    if matrix_is_telegram(event['state_key']):
        return

    user_id = event['state_key']
    content = event['content']

    sender = db.session.query(db.MatrixUser).filter_by(matrix_id=user_id).first()
    displayname = sender.name if sender else get_username(user_id)

    if content['membership'] == 'join':
        oldname = displayname

        try:
            displayname = content['displayname'] or get_username(user_id)
        except KeyError:
            displayname = get_username(user_id)

        if not sender:
            sender = db.MatrixUser(user_id, displayname)
        else:
            sender.name = displayname

        db.session.add(sender)
        db.session.commit()

        msg = None
        if 'unsigned' in event and 'prev_content' in event['unsigned']:
            prev = event['unsigned']['prev_content']
            if prev['membership'] == 'join':
                if 'displayname' in prev and prev['displayname']:
                    oldname = prev['displayname']

                msg = f'> {oldname} changed their display name to {displayname}'
        else:
            msg = f'> {displayname} has joined the room'

        if msg:
            return await group.send_text(msg)
    elif content['membership'] == 'leave':
        msg = f'< {displayname} has left the room'
        return await group.send_text(msg)
    elif content['membership'] == 'ban':
        msg = f'<! {displayname} was banned from the room'
        return await group.send_text(msg)


async def _matrix_event(event):
    """Handles an event from the Matrix homeserver."""

    # Discard old events, so the Telegram side is not spammed
    if 'age' in event and event['age'] > 600000:
        print('discarded event of age', event['age'])
        return

    # Retrieve information about the bridged room
    link = db.session.query(db.ChatLink).filter_by(matrix_room=event['room_id']).first()
    if not link and event['type'] != 'm.room.aliases':
        print(f'{event["room_id"]} isn\'t linked!', file=sys.stderr)
        return


    EVENT_HANDLERS = {
        'm.room.aliases': _matrix_on_aliases,
        'm.room.message': _matrix_on_message,
        'm.room.member': _matrix_on_member
    }

    try:
        event_handler = EVENT_HANDLERS[event['type']]
    except KeyError:
        print(f'No EVENT_HANDLER for {event["type"]}', file=sys.stderr)
        return

    try:
        group = TG_BOT.group(link.tg_room)
        await event_handler(event, link, group)
    except Exception as e:
        print(f'Got an exception ({e}) in group {group}')


async def matrix_transaction(request):
    """
    Handle a transaction sent by the homeserver.
    :param request: The request containing the transaction.
    :return: The response to send.
    """
    body = await request.json()
    for event in body['events']:
        _matrix_event(event)

    return create_response(200, {})


async def _matrix_request(method_fun, category, path, user_id, data=None, content_type=None):
    """
    Sends a request of type method_fun to the Matrix homeserver. Used as a
    helper function for the matrix_post, matrix_put, matrix_get and
    matrix_delete functions.
    """

    if content_type is None:
        content_type = 'application/octet-stream'

    if data is not None and isinstance(data, dict):
        data = json.dumps(data)
        content_type = 'application/json; charset=utf-8'

    params = {'access_token': AS_TOKEN}
    if user_id is not None:
        params['user_id'] = user_id

    async with method_fun('{}_matrix/{}/r0/{}'.format(MATRIX_HOST, quote(category), quote(path)),
                          params=params, data=data, headers={'Content-Type': content_type}) as response:
        if response.headers['Content-Type'].split(';')[0] == 'application/json':
            return await response.json()
        else:
            return await response.read()


def matrix_post(category, path, user_id, data, content_type=None):
    """Sends a POST request to the Matrix homeserver."""
    return _matrix_request(MATRIX_SESS.post, category, path, user_id, data,
                           content_type)


def matrix_put(category, path, user_id, data, content_type=None):
    """Sends a PUT request to the Matrix homeserver."""
    return _matrix_request(MATRIX_SESS.put, category, path, user_id, data,
                           content_type)


def matrix_get(category, path, user_id):
    """Sends a GET request to the Matrix homeserver."""
    return _matrix_request(MATRIX_SESS.get, category, path, user_id)


def matrix_delete(category, path, user_id):
    """Sends a DELETE request to the Matrix homeserver."""
    return _matrix_request(MATRIX_SESS.delete, category, path, user_id)


async def matrix_room(request):
    """Handles the room request from the Matrix homeserver."""

    room_alias = request.match_info['room_alias']
    args = parse_qs(urlparse(request.path_qs).query)
    print('Checking for {} | {}'.format(unquote(room_alias),
                                        args['access_token'][0]))

    try:
        if args['access_token'][0] != HS_TOKEN:
            return create_response(403, {'errcode': 'M_FORBIDDEN'})
    except KeyError:
        return create_response(401, {'errcode': 'NL.SIJMENSCHOON.TELEMATRIX_UNAUTHORIZED'})

    localpart = room_alias.split(':')[0]
    chat = '_'.join(localpart.split('_')[1:])

    # Look up the chat in the database
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat).first()
    if link:
        await matrix_post('client', 'createRoom', None, {'room_alias_name': localpart[1:]})
        response = create_response(200, {})
    else:
        response = create_response(404, {'errcode': 'NL.SIJMENSCHOON.TELEMATRIX_NOT_FOUND'})

    return response


def send_matrix_message(room_id, user_id, txn_id, **kwargs):
    """Sends a message to a Matrix room as user_id."""

    url = 'rooms/{}/send/m.room.message/{}'.format(room_id, txn_id)
    return matrix_put('client', url, user_id, kwargs)


async def upload_tgfile_to_matrix(file_id, user_id, mime='image/jpeg', convert_to=None):
    """
    Downloads a file from Telegram by its file_id and uploads it to Matrix,
    optionally converting it to another image format.
    """

    file_path = (await TG_BOT.get_file(file_id))['file_path']
    request = await TG_BOT.download_file(file_path)
    data = await request.read()

    if convert_to:
        image = Image.open(BytesIO(data))
        png_image = BytesIO(None)
        image.save(png_image, convert_to)

        j = await matrix_post('media', 'upload', user_id, png_image.getvalue(), mime)
        length = len(png_image.getvalue())
    else:
        j = await matrix_post('media', 'upload', user_id, data, mime)
        length = len(data)

    if 'content_uri' in j:
        return j['content_uri'], length
    else:
        return None, 0


async def register_join_matrix(chat, room_id, user_id):
    """
    Registers a bridged Telegram user on the homeserver and joins the linked
    room.
    """

    name = chat.sender['first_name']
    if 'last_name' in chat.sender:
        name += ' ' + chat.sender['last_name']
    name += ' (Telegram)'
    user = user_id.split(':')[0][1:]

    await matrix_post('client', 'register', None,
                      {'type': 'm.login.application_service', 'user': user})
    profile_photos = await TG_BOT.get_user_profile_photos(chat.sender['id'])
    try:
        pp_file_id = profile_photos['result']['photos'][0][-1]['file_id']
        pp_uri, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
        if pp_uri:
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id),
                             user_id, {'avatar_url': pp_uri})
    except IndexError:
        pass

    await matrix_put('client', 'profile/{}/displayname'.format(user_id),
                     user_id, {'displayname': name})
    await matrix_post('client', 'join/{}'.format(room_id), user_id, {})


async def update_matrix_displayname_avatar(tg_user):
    """
    Updates the Matrix display name and avatar to the ones used by a Telegram 
    user.
    """

    name = tg_user['first_name']
    if 'last_name' in tg_user:
        name += ' ' + tg_user['last_name']
    name += ' (Telegram)'
    user_id = USER_ID_FORMAT.format(tg_user['id'])

    db_user = db.session.query(db.TgUser).filter_by(tg_id=tg_user['id']).first()

    profile_photos = await TG_BOT.get_user_profile_photos(tg_user['id'])
    try:
        pp_file_id = profile_photos['result']['photos'][0][-1]['file_id']
    except KeyError:
        pp_file_id = None

    if db_user:
        if db_user.name != name:
            await matrix_put('client', 'profile/{}/displayname'.format(user_id), user_id, {'displayname': name})
            db_user.name = name
        if db_user.profile_pic_id != pp_file_id:
            if pp_file_id:
                pp_uri, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
                await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':pp_uri})
            else:
                await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':None})
            db_user.profile_pic_id = pp_file_id
    else:
        db_user = db.TgUser(tg_user['id'], name, pp_file_id)
        await matrix_put('client', 'profile/{}/displayname'.format(user_id), user_id, {'displayname': name})
        if pp_file_id:
            pp_uri, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':pp_uri})
        else:
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':None})
        db.session.add(db_user)
    db.session.commit()


@TG_BOT.handle('sticker')
async def aiotg_sticker(chat, sticker):
    """
    Receives a sticker from a Telegram group and sends it to the linked Matrix
    room as an image.
    """

    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if not link:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender)

    room_id = link.matrix_room
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}{}'.format(chat.message['message_id'], chat.id))

    file_id = sticker['file_id']
    uri, length = await upload_tgfile_to_matrix(file_id, user_id, 'image/png', 'PNG')

    info = {'mimetype': 'image/png', 'size': length, 'h': sticker['height'],
            'w': sticker['width']}
    body = 'Sticker_{}.png'.format(int(time() * 1000))

    if uri:
        j = await send_matrix_message(room_id, user_id, txn_id, body=body,
                                      url=uri, info=info, msgtype='m.image')

        if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
            await register_join_matrix(chat, room_id, user_id)
            await send_matrix_message(room_id, user_id, txn_id + 'join',
                                      body=body, url=uri, info=info,
                                      msgtype='m.image')

        if 'caption' in chat.message:
            await send_matrix_message(room_id, user_id, txn_id + 'caption',
                                      body=chat.message['caption'],
                                      msgtype='m.text')
        if 'event_id' in j:
            name = chat.sender['first_name']
            if 'last_name' in chat.sender:
                name += " " + chat.sender['last_name']

            name += " (Telegram)"
            message = db.Message(chat.message['chat']['id'], chat.message['message_id'],
                                 room_id, j['event_id'], name)

            db.session.add(message)
            db.session.commit()


@TG_BOT.handle('photo')
async def aiotg_photo(chat, photo):
    """
    Receives a photo from a Telegram group and sends it to the linked Matrix 
    room.
    """

    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if not link:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender)
    room_id = link.matrix_room
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}{}'.format(chat.message['message_id'], chat.id))

    file_id = photo[-1]['file_id']
    uri, length = await upload_tgfile_to_matrix(file_id, user_id)
    info = {'mimetype': 'image/jpeg', 'size': length, 'h': photo[-1]['height'],
            'w': photo[-1]['width']}
    body = 'Image_{}.jpg'.format(int(time() * 1000))

    if uri:
        j = await send_matrix_message(room_id, user_id, txn_id, body=body,
                                      url=uri, info=info, msgtype='m.image')

        if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
            await register_join_matrix(chat, room_id, user_id)
            await send_matrix_message(room_id, user_id, txn_id + 'join',
                                      body=body, url=uri, info=info,
                                      msgtype='m.image')

        if 'caption' in chat.message:
            await send_matrix_message(room_id, user_id, txn_id + 'caption',
                                      body=chat.message['caption'],
                                      msgtype='m.text')

        if 'event_id' in j:
            name = chat.sender['first_name']
            if 'last_name' in chat.sender:
                name += " " + chat.sender['last_name']

            name += " (Telegram)"
            message = db.Message(chat.message['chat']['id'], chat.message['message_id'],
                                 room_id, j['event_id'], name)

            db.session.add(message)
            db.session.commit()


@TG_BOT.command(r'/alias')
async def aiotg_alias(chat, _):
    """Handles the alias command from Telegram"""
    await chat.reply('The Matrix alias for this chat is #telegram_{}:{}'
                     .format(chat.id, MATRIX_HOST_BARE))


@TG_BOT.command(r'(?s)(.*)')
async def aiotg_message(chat, match):
    """
    Receives a message from the Telegram group and bridges it to the linked 
    Matrix room.
    """
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if link:
        room_id = link.matrix_room
    else:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender)
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}:{}'.format(chat.message['message_id'], chat.id))

    message = match.group(0)

    if 'forward_from' in chat.message:
        fw_from = chat.message['forward_from']
        if 'last_name' in fw_from:
            msg_from = '{} {} (Telegram)'.format(fw_from['first_name'],
                                                 fw_from['last_name'])
        else:
            msg_from = '{} (Telegram)'.format(fw_from['first_name'])

        quoted_msg = '\n'.join(['>{}'.format(x) for x in message.split('\n')])
        quoted_msg = 'Forwarded from {}:\n{}' \
                     .format(msg_from, quoted_msg)

        quoted_html = '<blockquote>{}</blockquote>' \
                      .format(html.escape(message).replace('\n', '<br />'))
        quoted_html = '<i>Forwarded from {}:</i>\n{}' \
                      .format(html.escape(msg_from), quoted_html)
        j = await send_matrix_message(room_id, user_id, txn_id,
                                      body=quoted_msg,
                                      formatted_body=quoted_html,
                                      format='org.matrix.custom.html',
                                      msgtype='m.text')

    elif 'reply_to_message' in chat.message:
        re_msg = chat.message['reply_to_message']
        if not 'text' in re_msg and not 'photo' in re_msg and not 'sticker' in re_msg:
            return

        msg_from = re_msg['from']
        if 'last_name' in msg_from:
            msg_from = f'{msg_from["first_name"]} {msg_from["last_name"]} (Telegram)'
        else:
            msg_from = f'{msg_from["first_name"]} (Telegram)'

        reply_mx_id = db.session.query(db.Message)\
                .filter_by(tg_group_id=chat.message['chat']['id'],
                           tg_message_id=chat.message['reply_to_message']['message_id']).first()

        html_message = html.escape(message).replace('\n', '<br />')
        if 'text' in re_msg:
            quoted_msg = '\n'.join(['>{}'.format(x)
                                    for x in re_msg['text'].split('\n')])
            quoted_html = '<blockquote>{}</blockquote>' \
                          .format(html.escape(re_msg['text'])
                                  .replace('\n', '<br />'))
        else:
            quoted_msg = ''
            quoted_html = ''

        if reply_mx_id:
            quoted_msg = 'Reply to {}:\n{}\n\n{}' \
                         .format(reply_mx_id.displayname, quoted_msg, message)
            quoted_html = '<i><a href="https://matrix.to/#/{}/{}">Reply to {}</a>:</i><br />{}<p>{}</p>' \
                          .format(html.escape(room_id), html.escape(reply_mx_id.matrix_event_id),
                                  html.escape(reply_mx_id.displayname), quoted_html, html_message)
        else:
            quoted_msg = 'Reply to {}:\n{}\n\n{}' \
                         .format(msg_from, quoted_msg, message)
            quoted_html = '<i>Reply to {}:</i><br />{}<p>{}</p>' \
                          .format(html.escape(msg_from),
                                  quoted_html, html_message)

        j = await send_matrix_message(room_id, user_id, txn_id,
                                      body=quoted_msg,
                                      formatted_body=quoted_html,
                                      format='org.matrix.custom.html',
                                      msgtype='m.text')
    else:
        j = await send_matrix_message(room_id, user_id, txn_id, body=message,
                                      msgtype='m.text')

    if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
        await register_join_matrix(chat, room_id, user_id)
        await asyncio.sleep(0.5)
        j = await send_matrix_message(room_id, user_id, txn_id + 'join',
                                      body=message, msgtype='m.text')
    elif 'event_id' in j:
        name = chat.sender['first_name']
        if 'last_name' in chat.sender:
            name += " " + chat.sender['last_name']

        name += " (Telegram)"
        message = db.Message(chat.message['chat']['id'], chat.message['message_id'],
                             room_id, j['event_id'], name)

        db.session.add(message)
        db.session.commit()


def main():
    """
    Main function to get the entire ball rolling.
    """
    logging.basicConfig(level=logging.WARNING)
    db.initialize(DATABASE_URL)

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(TG_BOT.loop())

    app = web.Application(loop=loop)
    app.router.add_route('GET', '/rooms/{room_alias}', matrix_room)
    app.router.add_route('PUT', '/transactions/{transaction}',
                         matrix_transaction)
    web.run_app(app, port=5000)


if __name__ == "__main__":
    main()
