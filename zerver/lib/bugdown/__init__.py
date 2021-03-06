# Zulip's main markdown implementation.  See docs/subsystems/markdown.md for
# detailed documentation on our markdown syntax.
from typing import (Any, Callable, Dict, Iterable, List, NamedTuple,
                    Optional, Set, Text, Tuple, TypeVar, Union)
from mypy_extensions import TypedDict
from typing.re import Match

import markdown
import logging
import traceback
import urllib
import re
import os
import html
import twitter
import platform
import time
import functools
import ujson
import xml.etree.cElementTree as etree
from xml.etree.cElementTree import Element, SubElement

from collections import deque, defaultdict

import requests

from django.core import mail
from django.conf import settings
from django.db.models import Q

from markdown.extensions import codehilite
from zerver.lib.bugdown import fenced_code
from zerver.lib.bugdown.fenced_code import FENCE_RE
from zerver.lib.camo import get_camo_url
from zerver.lib.mention import possible_mentions, \
    possible_user_group_mentions, extract_user_group
from zerver.lib.timeout import timeout, TimeoutExpired
from zerver.lib.cache import cache_with_key, NotFoundInCache
from zerver.lib.url_preview import preview as link_preview
from zerver.models import (
    all_realm_filters,
    get_active_streams,
    MAX_MESSAGE_LENGTH,
    Message,
    Realm,
    RealmFilter,
    realm_filters_for_realm,
    UserProfile,
    UserGroup,
    UserGroupMembership,
)
import zerver.lib.mention as mention
from zerver.lib.tex import render_tex

FullNameInfo = TypedDict('FullNameInfo', {
    'id': int,
    'email': Text,
    'full_name': Text,
})

# Format version of the bugdown rendering; stored along with rendered
# messages so that we can efficiently determine what needs to be re-rendered
version = 1

_T = TypeVar('_T')
ElementStringNone = Union[Element, Optional[Text]]

AVATAR_REGEX = r'!avatar\((?P<email>[^)]*)\)'
GRAVATAR_REGEX = r'!gravatar\((?P<email>[^)]*)\)'
EMOJI_REGEX = r'(?P<syntax>:[\w\-\+]+:)'

STREAM_LINK_REGEX = r"""
                     (?<![^\s'"\(,:<])            # Start after whitespace or specified chars
                     \#\*\*                       # and after hash sign followed by double asterisks
                         (?P<stream_name>[^\*]+)  # stream name can contain anything
                     \*\*                         # ends by double asterisks
                    """

class BugdownRenderingException(Exception):
    pass

def rewrite_if_relative_link(link: str) -> str:
    """ If the link points to a local destination we can just switch to that
    instead of opening a new tab. """

    if db_data:
        if link.startswith(db_data['realm_uri']):
            # +1 to skip the `/` before the hash link.
            return link[len(db_data['realm_uri']) + 1:]

    return link

def url_embed_preview_enabled_for_realm(message: Optional[Message]) -> bool:
    if message is not None:
        realm = message.get_realm()  # type: Optional[Realm]
    else:
        realm = None

    if not settings.INLINE_URL_EMBED_PREVIEW:
        return False
    if realm is None:
        return True
    return realm.inline_url_embed_preview

def image_preview_enabled_for_realm() -> bool:
    global current_message
    if current_message is not None:
        realm = current_message.get_realm()  # type: Optional[Realm]
    else:
        realm = None
    if not settings.INLINE_IMAGE_PREVIEW:
        return False
    if realm is None:
        return True
    return realm.inline_image_preview

def list_of_tlds() -> List[Text]:
    # HACK we manually blacklist a few domains
    blacklist = ['PY\n', "MD\n"]

    # tlds-alpha-by-domain.txt comes from http://data.iana.org/TLD/tlds-alpha-by-domain.txt
    tlds_file = os.path.join(os.path.dirname(__file__), 'tlds-alpha-by-domain.txt')
    tlds = [tld.lower().strip() for tld in open(tlds_file, 'r')
            if tld not in blacklist and not tld[0].startswith('#')]
    tlds.sort(key=len, reverse=True)
    return tlds

def walk_tree(root: Element,
              processor: Callable[[Element], Optional[_T]],
              stop_after_first: bool=False) -> List[_T]:
    results = []
    queue = deque([root])

    while queue:
        currElement = queue.popleft()
        for child in currElement.getchildren():
            if child.getchildren():
                queue.append(child)

            result = processor(child)
            if result is not None:
                results.append(result)
                if stop_after_first:
                    return results

    return results

ElementFamily = NamedTuple('ElementFamily', [
    ('grandparent', Optional[Element]),
    ('parent', Element),
    ('child', Element)
])

ResultWithFamily = NamedTuple('ResultWithFamily', [
    ('family', ElementFamily),
    ('result', Any)
])

def walk_tree_with_family(root: Element,
                          processor: Callable[[Element], Optional[_T]]
                          ) -> List[ResultWithFamily]:
    results = []

    queue = deque([{'parent': None, 'value': root}])
    while queue:
        currElementPair = queue.popleft()
        for child in currElementPair['value'].getchildren():
            if child.getchildren():
                queue.append({'parent': currElementPair, 'value': child})  # type: ignore  # Lack of Deque support in typing module for Python 3.4.3
            result = processor(child)
            if result is not None:
                if currElementPair['parent']:
                    grandparent = currElementPair['parent']['value']
                else:
                    grandparent = None
                family = ElementFamily(
                    grandparent=grandparent,
                    parent=currElementPair['value'],
                    child=child
                )

                results.append(ResultWithFamily(
                    family=family,
                    result=result
                ))

    return results

# height is not actually used
def add_a(
        root: Element,
        url: Text,
        link: Text,
        title: Optional[Text]=None,
        desc: Optional[Text]=None,
        class_attr: Text="message_inline_image",
        data_id: Optional[Text]=None,
        insertion_index: Optional[int]=None
) -> None:
    title = title if title is not None else url_filename(link)
    title = title if title else ""
    desc = desc if desc is not None else ""

    if insertion_index is not None:
        div = markdown.util.etree.Element("div")
        root.insert(insertion_index, div)
    else:
        div = markdown.util.etree.SubElement(root, "div")

    div.set("class", class_attr)
    a = markdown.util.etree.SubElement(div, "a")
    a.set("href", link)
    a.set("target", "_blank")
    a.set("title", title)
    if data_id is not None:
        a.set("data-id", data_id)
    img = markdown.util.etree.SubElement(a, "img")
    img.set("src", url)
    if class_attr == "message_inline_ref":
        summary_div = markdown.util.etree.SubElement(div, "div")
        title_div = markdown.util.etree.SubElement(summary_div, "div")
        title_div.set("class", "message_inline_image_title")
        title_div.text = title
        desc_div = markdown.util.etree.SubElement(summary_div, "desc")
        desc_div.set("class", "message_inline_image_desc")

def add_embed(root: Element, link: Text, extracted_data: Dict[Text, Any]) -> None:
    container = markdown.util.etree.SubElement(root, "div")
    container.set("class", "message_embed")

    img_link = extracted_data.get('image')
    if img_link:
        parsed_img_link = urllib.parse.urlparse(img_link)
        # Append domain where relative img_link url is given
        if not parsed_img_link.netloc:
            parsed_url = urllib.parse.urlparse(link)
            domain = '{url.scheme}://{url.netloc}/'.format(url=parsed_url)
            img_link = urllib.parse.urljoin(domain, img_link)
        img = markdown.util.etree.SubElement(container, "a")
        img.set("style", "background-image: url(" + img_link + ")")
        img.set("href", link)
        img.set("target", "_blank")
        img.set("class", "message_embed_image")

    data_container = markdown.util.etree.SubElement(container, "div")
    data_container.set("class", "data-container")

    title = extracted_data.get('title')
    if title:
        title_elm = markdown.util.etree.SubElement(data_container, "div")
        title_elm.set("class", "message_embed_title")
        a = markdown.util.etree.SubElement(title_elm, "a")
        a.set("href", link)
        a.set("target", "_blank")
        a.set("title", title)
        a.text = title
    description = extracted_data.get('description')
    if description:
        description_elm = markdown.util.etree.SubElement(data_container, "div")
        description_elm.set("class", "message_embed_description")
        description_elm.text = description

def add_vimeo_preview(root: Element, link: Text, extracted_data: Dict[Text, Any], vm_id: Text) -> None:
    container = markdown.util.etree.SubElement(root, "div")
    container.set("class", "vimeo-video message_inline_image")

    img_link = extracted_data.get('image')
    if img_link:
        parsed_img_link = urllib.parse.urlparse(img_link)
        # Append domain where relative img_link url is given
        if not parsed_img_link.netloc:
            parsed_url = urllib.parse.urlparse(link)
            domain = '{url.scheme}://{url.netloc}/'.format(url=parsed_url)
            img_link = urllib.parse.urljoin(domain, img_link)
        anchor = markdown.util.etree.SubElement(container, "a")
        anchor.set("href", link)
        anchor.set("target", "_blank")
        anchor.set("data-id", vm_id)
        anchor.set("title", link)
        img = markdown.util.etree.SubElement(anchor, "img")
        img.set("src", img_link)

@cache_with_key(lambda tweet_id: tweet_id, cache_name="database", with_statsd_key="tweet_data")
def fetch_tweet_data(tweet_id: Text) -> Optional[Dict[Text, Any]]:
    if settings.TEST_SUITE:
        from . import testing_mocks
        res = testing_mocks.twitter(tweet_id)
    else:
        creds = {
            'consumer_key': settings.TWITTER_CONSUMER_KEY,
            'consumer_secret': settings.TWITTER_CONSUMER_SECRET,
            'access_token_key': settings.TWITTER_ACCESS_TOKEN_KEY,
            'access_token_secret': settings.TWITTER_ACCESS_TOKEN_SECRET,
        }
        if not all(creds.values()):
            return None

        try:
            api = twitter.Api(tweet_mode='extended', **creds)
            # Sometimes Twitter hangs on responses.  Timing out here
            # will cause the Tweet to go through as-is with no inline
            # preview, rather than having the message be rejected
            # entirely. This timeout needs to be less than our overall
            # formatting timeout.
            tweet = timeout(3, api.GetStatus, tweet_id)
            res = tweet.AsDict()
        except AttributeError:
            logging.error('Unable to load twitter api, you may have the wrong '
                          'library installed, see https://github.com/zulip/zulip/issues/86')
            return None
        except TimeoutExpired as e:
            # We'd like to try again later and not cache the bad result,
            # so we need to re-raise the exception (just as though
            # we were being rate-limited)
            raise
        except twitter.TwitterError as e:
            t = e.args[0]
            if len(t) == 1 and ('code' in t[0]) and (t[0]['code'] == 34):
                # Code 34 means that the message doesn't exist; return
                # None so that we will cache the error
                return None
            elif len(t) == 1 and ('code' in t[0]) and (t[0]['code'] == 88 or
                                                       t[0]['code'] == 130):
                # Code 88 means that we were rate-limited and 130
                # means Twitter is having capacity issues; either way
                # just raise the error so we don't cache None and will
                # try again later.
                raise
            else:
                # It's not clear what to do in cases of other errors,
                # but for now it seems reasonable to log at error
                # level (so that we get notified), but then cache the
                # failure to proceed with our usual work
                logging.error(traceback.format_exc())
                return None
    return res

HEAD_START_RE = re.compile('^head[ >]')
HEAD_END_RE = re.compile('^/head[ >]')
META_START_RE = re.compile('^meta[ >]')
META_END_RE = re.compile('^/meta[ >]')

def fetch_open_graph_image(url: Text) -> Optional[Dict[str, Any]]:
    in_head = False
    # HTML will auto close meta tags, when we start the next tag add
    # a closing tag if it has not been closed yet.
    last_closed = True
    head = []
    # TODO: What if response content is huge? Should we get headers first?
    try:
        content = requests.get(url, timeout=1).text
    except Exception:
        return None
    # Extract the head and meta tags
    # All meta tags are self closing, have no children or are closed
    # automatically.
    for part in content.split('<'):
        if not in_head and HEAD_START_RE.match(part):
            # Started the head node output it to have a document root
            in_head = True
            head.append('<head>')
        elif in_head and HEAD_END_RE.match(part):
            # Found the end of the head close any remaining tag then stop
            # processing
            in_head = False
            if not last_closed:
                last_closed = True
                head.append('</meta>')
            head.append('</head>')
            break

        elif in_head and META_START_RE.match(part):
            # Found a meta node copy it
            if not last_closed:
                head.append('</meta>')
                last_closed = True
            head.append('<')
            head.append(part)
            if '/>' not in part:
                last_closed = False

        elif in_head and META_END_RE.match(part):
            # End of a meta node just copy it to close the tag
            head.append('<')
            head.append(part)
            last_closed = True

    try:
        doc = etree.fromstring(''.join(head))
    except etree.ParseError:
        return None
    og_image = doc.find('meta[@property="og:image"]')
    og_title = doc.find('meta[@property="og:title"]')
    og_desc = doc.find('meta[@property="og:description"]')
    title = None
    desc = None
    if og_image is not None:
        image = og_image.get('content')
    else:
        return None
    if og_title is not None:
        title = og_title.get('content')
    if og_desc is not None:
        desc = og_desc.get('content')
    return {'image': image, 'title': title, 'desc': desc}

def get_tweet_id(url: Text) -> Optional[Text]:
    parsed_url = urllib.parse.urlparse(url)
    if not (parsed_url.netloc == 'twitter.com' or parsed_url.netloc.endswith('.twitter.com')):
        return None
    to_match = parsed_url.path
    # In old-style twitter.com/#!/wdaher/status/1231241234-style URLs,
    # we need to look at the fragment instead
    if parsed_url.path == '/' and len(parsed_url.fragment) > 5:
        to_match = parsed_url.fragment

    tweet_id_match = re.match(r'^!?/.*?/status(es)?/(?P<tweetid>\d{10,18})(/photo/[0-9])?/?$', to_match)
    if not tweet_id_match:
        return None
    return tweet_id_match.group("tweetid")

class InlineHttpsProcessor(markdown.treeprocessors.Treeprocessor):
    def run(self, root: Element) -> None:
        # Get all URLs from the blob
        found_imgs = walk_tree(root, lambda e: e if e.tag == "img" else None)
        for img in found_imgs:
            url = img.get("src")
            if not url.startswith("http://"):
                # Don't rewrite images on our own site (e.g. emoji).
                continue
            img.set("src", get_camo_url(url))

class BacktickPattern(markdown.inlinepatterns.Pattern):
    """ Return a `<code>` element containing the matching text. """
    def __init__(self, pattern):
        # type: (Text) -> None
        markdown.inlinepatterns.Pattern.__init__(self, pattern)
        self.ESCAPED_BSLASH = '%s%s%s' % (markdown.util.STX, ord('\\'), markdown.util.ETX)
        self.tag = 'code'

    def handleMatch(self, m):
        # type: (Match[Text]) -> Union[Text, Element]
        if m.group(4):
            el = markdown.util.etree.Element(self.tag)
            # Modified to not strip whitespace
            el.text = markdown.util.AtomicString(m.group(4))
            return el
        else:
            return m.group(2).replace('\\\\', self.ESCAPED_BSLASH)

class InlineInterestingLinkProcessor(markdown.treeprocessors.Treeprocessor):
    TWITTER_MAX_IMAGE_HEIGHT = 400
    TWITTER_MAX_TO_PREVIEW = 3

    def __init__(self, md: markdown.Markdown, bugdown: 'Bugdown') -> None:
        # Passing in bugdown for access to config to check if realm is zulip.com
        self.bugdown = bugdown
        markdown.treeprocessors.Treeprocessor.__init__(self, md)

    def get_actual_image_url(self, url: Text) -> Text:
        # Add specific per-site cases to convert image-preview urls to image urls.
        # See https://github.com/zulip/zulip/issues/4658 for more information
        parsed_url = urllib.parse.urlparse(url)
        if (parsed_url.netloc == 'github.com' or parsed_url.netloc.endswith('.github.com')):
            # https://github.com/zulip/zulip/blob/master/static/images/logo/zulip-icon-128x128.png ->
            # https://raw.githubusercontent.com/zulip/zulip/master/static/images/logo/zulip-icon-128x128.png
            split_path = parsed_url.path.split('/')
            if len(split_path) > 3 and split_path[3] == "blob":
                return urllib.parse.urljoin('https://raw.githubusercontent.com',
                                            '/'.join(split_path[0:3] + split_path[4:]))

        return url

    def is_image(self, url: Text) -> bool:
        if not image_preview_enabled_for_realm():
            return False
        parsed_url = urllib.parse.urlparse(url)
        # List from http://support.google.com/chromeos/bin/answer.py?hl=en&answer=183093
        for ext in [".bmp", ".gif", ".jpg", "jpeg", ".png", ".webp"]:
            if parsed_url.path.lower().endswith(ext):
                return True
        return False

    def dropbox_image(self, url: Text) -> Optional[Dict[str, Any]]:
        # TODO: The returned Dict could possibly be a TypedDict in future.
        parsed_url = urllib.parse.urlparse(url)
        if (parsed_url.netloc == 'dropbox.com' or parsed_url.netloc.endswith('.dropbox.com')):
            is_album = parsed_url.path.startswith('/sc/') or parsed_url.path.startswith('/photos/')
            # Only allow preview Dropbox shared links
            if not (parsed_url.path.startswith('/s/') or
                    parsed_url.path.startswith('/sh/') or
                    is_album):
                return None

            # Try to retrieve open graph protocol info for a preview
            # This might be redundant right now for shared links for images.
            # However, we might want to make use of title and description
            # in the future. If the actual image is too big, we might also
            # want to use the open graph image.
            image_info = fetch_open_graph_image(url)

            is_image = is_album or self.is_image(url)

            # If it is from an album or not an actual image file,
            # just use open graph image.
            if is_album or not is_image:
                # Failed to follow link to find an image preview so
                # use placeholder image and guess filename
                if image_info is None:
                    return None

                image_info["is_image"] = is_image
                return image_info

            # Otherwise, try to retrieve the actual image.
            # This is because open graph image from Dropbox may have padding
            # and gifs do not work.
            # TODO: What if image is huge? Should we get headers first?
            if image_info is None:
                image_info = dict()
            image_info['is_image'] = True
            parsed_url_list = list(parsed_url)
            parsed_url_list[4] = "dl=1"  # Replaces query
            image_info["image"] = urllib.parse.urlunparse(parsed_url_list)

            return image_info
        return None

    def youtube_id(self, url: Text) -> Optional[Text]:
        if not image_preview_enabled_for_realm():
            return None
        # Youtube video id extraction regular expression from http://pastebin.com/KyKAFv1s
        # If it matches, match.group(2) is the video id.
        youtube_re = r'^((?:https?://)?(?:youtu\.be/|(?:\w+\.)?youtube(?:-nocookie)?\.com/)' + \
                     r'(?:(?:(?:v|embed)/)|(?:(?:watch(?:_popup)?(?:\.php)?)?(?:\?|#!?)(?:.+&)?v=)))' + \
                     r'?([0-9A-Za-z_-]+)(?(1).+)?$'
        match = re.match(youtube_re, url)
        if match is None:
            return None
        return match.group(2)

    def youtube_image(self, url: Text) -> Optional[Text]:
        yt_id = self.youtube_id(url)

        if yt_id is not None:
            return "https://i.ytimg.com/vi/%s/default.jpg" % (yt_id,)
        return None

    def vimeo_id(self, url: Text) -> Optional[Text]:
        if not image_preview_enabled_for_realm():
            return None
        #(http|https)?:\/\/(www\.)?vimeo.com\/(?:channels\/(?:\w+\/)?|groups\/([^\/]*)\/videos\/|)(\d+)(?:|\/\?)
        # If it matches, match.group('id') is the video id.

        vimeo_re = r'^((http|https)?:\/\/(www\.)?vimeo.com\/' + \
                   r'(?:channels\/(?:\w+\/)?|groups\/' + \
                   r'([^\/]*)\/videos\/|)(\d+)(?:|\/\?))$'
        match = re.match(vimeo_re, url)
        if match is None:
            return None
        return match.group(5)

    def vimeo_image(self, url: Text) -> Optional[Text]:
        vm_id = self.vimeo_id(url)

        if vm_id is not None:
            return "http://i.vimeocdn.com/video/%s.jpg" % (vm_id,)
        return None

    def twitter_text(self, text: Text,
                     urls: List[Dict[Text, Text]],
                     user_mentions: List[Dict[Text, Any]],
                     media: List[Dict[Text, Any]]) -> Element:
        """
        Use data from the twitter API to turn links, mentions and media into A
        tags. Also convert unicode emojis to images.

        This works by using the urls, user_mentions and media data from
        the twitter API and searching for unicode emojis in the text using
        `unicode_emoji_regex`.

        The first step is finding the locations of the URLs, mentions, media and
        emoji in the text. For each match we build a dictionary with type, the start
        location, end location, the URL to link to, and the text(codepoint and title
        in case of emojis) to be used in the link(image in case of emojis).

        Next we sort the matches by start location. And for each we add the
        text from the end of the last link to the start of the current link to
        the output. The text needs to added to the text attribute of the first
        node (the P tag) or the tail the last link created.

        Finally we add any remaining text to the last node.
        """

        to_process = []  # type: List[Dict[Text, Any]]
        # Build dicts for URLs
        for url_data in urls:
            short_url = url_data["url"]
            full_url = url_data["expanded_url"]
            for match in re.finditer(re.escape(short_url), text, re.IGNORECASE):
                to_process.append({
                    'type': 'url',
                    'start': match.start(),
                    'end': match.end(),
                    'url': short_url,
                    'text': full_url,
                })
        # Build dicts for mentions
        for user_mention in user_mentions:
            screen_name = user_mention['screen_name']
            mention_string = '@' + screen_name
            for match in re.finditer(re.escape(mention_string), text, re.IGNORECASE):
                to_process.append({
                    'type': 'mention',
                    'start': match.start(),
                    'end': match.end(),
                    'url': 'https://twitter.com/' + urllib.parse.quote(screen_name),
                    'text': mention_string,
                })
        # Build dicts for media
        for media_item in media:
            short_url = media_item['url']
            expanded_url = media_item['expanded_url']
            for match in re.finditer(re.escape(short_url), text, re.IGNORECASE):
                to_process.append({
                    'type': 'media',
                    'start': match.start(),
                    'end': match.end(),
                    'url': short_url,
                    'text': expanded_url,
                })
        # Build dicts for emojis
        for match in re.finditer(unicode_emoji_regex, text, re.IGNORECASE):
            orig_syntax = match.group('syntax')
            codepoint = unicode_emoji_to_codepoint(orig_syntax)
            if codepoint in codepoint_to_name:
                display_string = ':' + codepoint_to_name[codepoint] + ':'
                to_process.append({
                    'type': 'emoji',
                    'start': match.start(),
                    'end': match.end(),
                    'codepoint': codepoint,
                    'title': display_string,
                })

        to_process.sort(key=lambda x: x['start'])
        p = current_node = markdown.util.etree.Element('p')

        def set_text(text: Text) -> None:
            """
            Helper to set the text or the tail of the current_node
            """
            if current_node == p:
                current_node.text = text
            else:
                current_node.tail = text

        current_index = 0
        for item in to_process:
            # The text we want to link starts in already linked text skip it
            if item['start'] < current_index:
                continue
            # Add text from the end of last link to the start of the current
            # link
            set_text(text[current_index:item['start']])
            current_index = item['end']
            if item['type'] != 'emoji':
                current_node = elem = url_to_a(item['url'], item['text'])
            else:
                current_node = elem = make_emoji(item['codepoint'], item['title'])
            p.append(elem)

        # Add any unused text
        set_text(text[current_index:])
        return p

    def twitter_link(self, url: Text) -> Optional[Element]:
        tweet_id = get_tweet_id(url)

        if tweet_id is None:
            return None

        try:
            res = fetch_tweet_data(tweet_id)
            if res is None:
                return None
            user = res['user']  # type: Dict[Text, Any]
            tweet = markdown.util.etree.Element("div")
            tweet.set("class", "twitter-tweet")
            img_a = markdown.util.etree.SubElement(tweet, 'a')
            img_a.set("href", url)
            img_a.set("target", "_blank")
            profile_img = markdown.util.etree.SubElement(img_a, 'img')
            profile_img.set('class', 'twitter-avatar')
            # For some reason, for, e.g. tweet 285072525413724161,
            # python-twitter does not give us a
            # profile_image_url_https, but instead puts that URL in
            # profile_image_url. So use _https if available, but fall
            # back gracefully.
            image_url = user.get('profile_image_url_https', user['profile_image_url'])
            profile_img.set('src', image_url)

            text = html.unescape(res['full_text'])
            urls = res.get('urls', [])
            user_mentions = res.get('user_mentions', [])
            media = res.get('media', [])  # type: List[Dict[Text, Any]]
            p = self.twitter_text(text, urls, user_mentions, media)
            tweet.append(p)

            span = markdown.util.etree.SubElement(tweet, 'span')
            span.text = "- %s (@%s)" % (user['name'], user['screen_name'])

            # Add image previews
            for media_item in media:
                # Only photos have a preview image
                if media_item['type'] != 'photo':
                    continue

                # Find the image size that is smaller than
                # TWITTER_MAX_IMAGE_HEIGHT px tall or the smallest
                size_name_tuples = list(media_item['sizes'].items())
                size_name_tuples.sort(reverse=True,
                                      key=lambda x: x[1]['h'])
                for size_name, size in size_name_tuples:
                    if size['h'] < self.TWITTER_MAX_IMAGE_HEIGHT:
                        break

                media_url = '%s:%s' % (media_item['media_url_https'], size_name)
                img_div = markdown.util.etree.SubElement(tweet, 'div')
                img_div.set('class', 'twitter-image')
                img_a = markdown.util.etree.SubElement(img_div, 'a')
                img_a.set('href', media_item['url'])
                img_a.set('target', '_blank')
                img_a.set('title', media_item['url'])
                img = markdown.util.etree.SubElement(img_a, 'img')
                img.set('src', media_url)

            return tweet
        except Exception:
            # We put this in its own try-except because it requires external
            # connectivity. If Twitter flakes out, we don't want to not-render
            # the entire message; we just want to not show the Twitter preview.
            logging.warning(traceback.format_exc())
            return None

    def get_url_data(self, e: Element) -> Optional[Tuple[Text, Text]]:
        if e.tag == "a":
            if e.text is not None:
                return (e.get("href"), e.text)
            return (e.get("href"), e.get("href"))
        return None

    def handle_image_inlining(self, root: Element, found_url: ResultWithFamily) -> None:
        grandparent = found_url.family.grandparent
        parent = found_url.family.parent
        ahref_element = found_url.family.child
        (url, text) = found_url.result
        actual_url = self.get_actual_image_url(url)

        # url != text usually implies a named link, which we opt not to remove
        url_eq_text = (url == text)

        if parent.tag == 'li':
            add_a(parent, self.get_actual_image_url(url), url, title=text)
            if not parent.text and not ahref_element.tail and url_eq_text:
                parent.remove(ahref_element)

        elif parent.tag == 'p':
            parent_index = None
            for index, uncle in enumerate(grandparent.getchildren()):
                if uncle is parent:
                    parent_index = index
                    break

            if parent_index is not None:
                ins_index = self.find_proper_insertion_index(grandparent, parent, parent_index)
                add_a(grandparent, actual_url, url, title=text, insertion_index=ins_index)

            else:
                # We're not inserting after parent, since parent not found.
                # Append to end of list of grandparent's children as normal
                add_a(grandparent, actual_url, url, title=text)

            # If link is alone in a paragraph, delete paragraph containing it
            if (len(parent.getchildren()) == 1 and
                    (not parent.text or parent.text == "\n") and
                    not ahref_element.tail and
                    url_eq_text):
                grandparent.remove(parent)

        else:
            # If none of the above criteria match, fall back to old behavior
            add_a(root, actual_url, url, title=text)

    def find_proper_insertion_index(self, grandparent: Element, parent: Element,
                                    parent_index_in_grandparent: int) -> int:
        # If there are several inline images from same paragraph, ensure that
        # they are in correct (and not opposite) order by inserting after last
        # inline image from paragraph 'parent'

        uncles = grandparent.getchildren()
        parent_links = [ele.attrib['href'] for ele in parent.iter(tag="a")]
        insertion_index = parent_index_in_grandparent

        while True:
            insertion_index += 1
            if insertion_index >= len(uncles):
                return insertion_index

            uncle = uncles[insertion_index]
            inline_image_classes = ['message_inline_image', 'message_inline_ref']
            if (
                uncle.tag != 'div' or
                'class' not in uncle.keys() or
                uncle.attrib['class'] not in inline_image_classes
            ):
                return insertion_index

            uncle_link = list(uncle.iter(tag="a"))[0].attrib['href']
            if uncle_link not in parent_links:
                return insertion_index

    def run(self, root: Element) -> None:
        # Get all URLs from the blob
        found_urls = walk_tree_with_family(root, self.get_url_data)

        # If there are more than 5 URLs in the message, don't do inline previews
        if len(found_urls) == 0 or len(found_urls) > 5:
            return

        rendered_tweet_count = 0

        for found_url in found_urls:
            (url, text) = found_url.result
            dropbox_image = self.dropbox_image(url)

            if dropbox_image is not None:
                class_attr = "message_inline_ref"
                is_image = dropbox_image["is_image"]
                if is_image:
                    class_attr = "message_inline_image"
                    # Not making use of title and description of images
                add_a(root, dropbox_image['image'], url,
                      title=dropbox_image.get('title', ""),
                      desc=dropbox_image.get('desc', ""),
                      class_attr=class_attr)
                continue
            if self.is_image(url):
                self.handle_image_inlining(root, found_url)
                continue
            if get_tweet_id(url) is not None:
                if rendered_tweet_count >= self.TWITTER_MAX_TO_PREVIEW:
                    # Only render at most one tweet per message
                    continue
                twitter_data = self.twitter_link(url)
                if twitter_data is None:
                    # This link is not actually a tweet known to twitter
                    continue
                rendered_tweet_count += 1
                div = markdown.util.etree.SubElement(root, "div")
                div.set("class", "inline-preview-twitter")
                div.insert(0, twitter_data)
                continue
            youtube = self.youtube_image(url)
            if youtube is not None:
                yt_id = self.youtube_id(url)
                add_a(root, youtube, url, None, None, "youtube-video message_inline_image", yt_id)
                continue

            global db_data

            if db_data and db_data['sent_by_bot']:
                continue

            if current_message is None or not url_embed_preview_enabled_for_realm(current_message):
                continue
            try:
                extracted_data = link_preview.link_embed_data_from_cache(url)
            except NotFoundInCache:
                current_message.links_for_preview.add(url)
                continue
            vimeo = self.vimeo_image(url)
            if extracted_data:
                if vimeo is not None:
                    vm_id = self.vimeo_id(url)
                    add_vimeo_preview(root, url, extracted_data, vm_id)
                    continue
                else:
                    add_embed(root, url, extracted_data)


class Avatar(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match: Match[Text]) -> Optional[Element]:
        img = markdown.util.etree.Element('img')
        email_address = match.group('email')
        email = email_address.strip().lower()
        profile_id = None

        if db_data is not None:
            user_dict = db_data['email_info'].get(email)
            if user_dict is not None:
                profile_id = user_dict['id']

        img.set('class', 'message_body_gravatar')
        img.set('src', '/avatar/{0}?s=30'.format(profile_id or email))
        img.set('title', email)
        img.set('alt', email)
        return img

def possible_avatar_emails(content: Text) -> Set[Text]:
    emails = set()
    for regex in [AVATAR_REGEX, GRAVATAR_REGEX]:
        matches = re.findall(regex, content)
        for email in matches:
            if email:
                emails.add(email)

    return emails

path_to_name_to_codepoint = os.path.join(settings.STATIC_ROOT,
                                         "generated", "emoji", "name_to_codepoint.json")
with open(path_to_name_to_codepoint) as name_to_codepoint_file:
    name_to_codepoint = ujson.load(name_to_codepoint_file)

path_to_codepoint_to_name = os.path.join(settings.STATIC_ROOT,
                                         "generated", "emoji", "codepoint_to_name.json")
with open(path_to_codepoint_to_name) as codepoint_to_name_file:
    codepoint_to_name = ujson.load(codepoint_to_name_file)

# All of our emojis(non ZWJ sequences) belong to one of these unicode blocks:
# \U0001f100-\U0001f1ff - Enclosed Alphanumeric Supplement
# \U0001f200-\U0001f2ff - Enclosed Ideographic Supplement
# \U0001f300-\U0001f5ff - Miscellaneous Symbols and Pictographs
# \U0001f600-\U0001f64f - Emoticons (Emoji)
# \U0001f680-\U0001f6ff - Transport and Map Symbols
# \U0001f900-\U0001f9ff - Supplemental Symbols and Pictographs
# \u2000-\u206f         - General Punctuation
# \u2300-\u23ff         - Miscellaneous Technical
# \u2400-\u243f         - Control Pictures
# \u2440-\u245f         - Optical Character Recognition
# \u2460-\u24ff         - Enclosed Alphanumerics
# \u2500-\u257f         - Box Drawing
# \u2580-\u259f         - Block Elements
# \u25a0-\u25ff         - Geometric Shapes
# \u2600-\u26ff         - Miscellaneous Symbols
# \u2700-\u27bf         - Dingbats
# \u2900-\u297f         - Supplemental Arrows-B
# \u2b00-\u2bff         - Miscellaneous Symbols and Arrows
# \u3000-\u303f         - CJK Symbols and Punctuation
# \u3200-\u32ff         - Enclosed CJK Letters and Months
unicode_emoji_regex = '(?P<syntax>['\
    '\U0001F100-\U0001F64F'    \
    '\U0001F680-\U0001F6FF'    \
    '\U0001F900-\U0001F9FF'    \
    '\u2000-\u206F'            \
    '\u2300-\u27BF'            \
    '\u2900-\u297F'            \
    '\u2B00-\u2BFF'            \
    '\u3000-\u303F'            \
    '\u3200-\u32FF'            \
    '])'
# The equivalent JS regex is \ud83c[\udd00-\udfff]|\ud83d[\udc00-\ude4f]|\ud83d[\ude80-\udeff]|
# \ud83e[\udd00-\uddff]|[\u2000-\u206f]|[\u2300-\u27bf]|[\u2b00-\u2bff]|[\u3000-\u303f]|
# [\u3200-\u32ff]. See below comments for explanation. The JS regex is used by marked.js for
# frontend unicode emoji processing.
# The JS regex \ud83c[\udd00-\udfff]|\ud83d[\udc00-\ude4f] represents U0001f100-\U0001f64f
# The JS regex \ud83d[\ude80-\udeff] represents \U0001f680-\U0001f6ff
# The JS regex \ud83e[\udd00-\uddff] represents \U0001f900-\U0001f9ff
# The JS regex [\u2000-\u206f] represents \u2000-\u206f
# The JS regex [\u2300-\u27bf] represents \u2300-\u27bf
# Similarly other JS regexes can be mapped to the respective unicode blocks.
# For more information, please refer to the following article:
# http://crocodillon.com/blog/parsing-emoji-unicode-in-javascript

def make_emoji(codepoint: Text, display_string: Text) -> Element:
    # Replace underscore in emoji's title with space
    title = display_string[1:-1].replace("_", " ")
    span = markdown.util.etree.Element('span')
    span.set('class', 'emoji emoji-%s' % (codepoint,))
    span.set('title', title)
    span.text = display_string
    return span

def make_realm_emoji(src: Text, display_string: Text) -> Element:
    elt = markdown.util.etree.Element('img')
    elt.set('src', src)
    elt.set('class', 'emoji')
    elt.set("alt", display_string)
    elt.set("title", display_string[1:-1].replace("_", " "))
    return elt

def unicode_emoji_to_codepoint(unicode_emoji: Text) -> Text:
    codepoint = hex(ord(unicode_emoji))[2:]
    # Unicode codepoints are minimum of length 4, padded
    # with zeroes if the length is less than zero.
    while len(codepoint) < 4:
        codepoint = '0' + codepoint
    return codepoint

class UnicodeEmoji(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match: Match[Text]) -> Optional[Element]:
        orig_syntax = match.group('syntax')
        codepoint = unicode_emoji_to_codepoint(orig_syntax)
        if codepoint in codepoint_to_name:
            display_string = ':' + codepoint_to_name[codepoint] + ':'
            return make_emoji(codepoint, display_string)
        else:
            return None

class Emoji(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match: Match[Text]) -> Optional[Element]:
        orig_syntax = match.group("syntax")
        name = orig_syntax[1:-1]

        realm_emoji = {}  # type: Dict[Text, Dict[str, Text]]
        if db_data is not None:
            realm_emoji = db_data['realm_emoji']

        if current_message and name in realm_emoji and not realm_emoji[name]['deactivated']:
            return make_realm_emoji(realm_emoji[name]['source_url'], orig_syntax)
        elif name == 'zulip':
            return make_realm_emoji('/static/generated/emoji/images/emoji/unicode/zulip.png', orig_syntax)
        elif name in name_to_codepoint:
            return make_emoji(name_to_codepoint[name], orig_syntax)
        else:
            return None

def content_has_emoji_syntax(content: Text) -> bool:
    return re.search(EMOJI_REGEX, content) is not None

class StreamSubscribeButton(markdown.inlinepatterns.Pattern):
    # This markdown extension has required javascript in
    # static/js/custom_markdown.js
    def handleMatch(self, match: Match[Text]) -> Element:
        stream_name = match.group('stream_name')
        stream_name = stream_name.replace('\\)', ')').replace('\\\\', '\\')

        span = markdown.util.etree.Element('span')
        span.set('class', 'inline-subscribe')
        span.set('data-stream-name', stream_name)

        button = markdown.util.etree.SubElement(span, 'button')
        button.text = 'Subscribe to ' + stream_name
        button.set('class', 'inline-subscribe-button btn')

        error = markdown.util.etree.SubElement(span, 'span')
        error.set('class', 'inline-subscribe-error')

        return span

class ModalLink(markdown.inlinepatterns.Pattern):
    """
    A pattern that allows including in-app modal links in messages.
    """

    def handleMatch(self, match: Match[Text]) -> Element:
        relative_url = match.group('relative_url')
        text = match.group('text')

        a_tag = markdown.util.etree.Element("a")
        a_tag.set("href", relative_url)
        a_tag.set("title", relative_url)
        a_tag.text = text

        return a_tag

class Tex(markdown.inlinepatterns.Pattern):
    def handleMatch(self, match: Match[Text]) -> Element:
        rendered = render_tex(match.group('body'), is_inline=True)
        if rendered is not None:
            return etree.fromstring(rendered.encode('utf-8'))
        else:  # Something went wrong while rendering
            span = markdown.util.etree.Element('span')
            span.set('class', 'tex-error')
            span.text = '$$' + match.group('body') + '$$'
            return span

upload_title_re = re.compile("^(https?://[^/]*)?(/user_uploads/\\d+)(/[^/]*)?/[^/]*/(?P<filename>[^/]*)$")
def url_filename(url: Text) -> Text:
    """Extract the filename if a URL is an uploaded file, or return the original URL"""
    match = upload_title_re.match(url)
    if match:
        return match.group('filename')
    else:
        return url

def fixup_link(link: markdown.util.etree.Element, target_blank: bool=True) -> None:
    """Set certain attributes we want on every link."""
    if target_blank:
        link.set('target', '_blank')
    link.set('title', url_filename(link.get('href')))


def sanitize_url(url: Text) -> Optional[Text]:
    """
    Sanitize a url against xss attacks.
    See the docstring on markdown.inlinepatterns.LinkPattern.sanitize_url.
    """
    try:
        parts = urllib.parse.urlparse(url.replace(' ', '%20'))
        scheme, netloc, path, params, query, fragment = parts
    except ValueError:
        # Bad url - so bad it couldn't be parsed.
        return ''

    # If there is no scheme or netloc and there is a '@' in the path,
    # treat it as a mailto: and set the appropriate scheme
    if scheme == '' and netloc == '' and '@' in path:
        scheme = 'mailto'
    elif scheme == '' and netloc == '' and len(path) > 0 and path[0] == '/':
        # Allow domain-relative links
        return urllib.parse.urlunparse(('', '', path, params, query, fragment))
    elif (scheme, netloc, path, params, query) == ('', '', '', '', '') and len(fragment) > 0:
        # Allow fragment links
        return urllib.parse.urlunparse(('', '', '', '', '', fragment))

    # Zulip modification: If scheme is not specified, assume http://
    # We re-enter sanitize_url because netloc etc. need to be re-parsed.
    if not scheme:
        return sanitize_url('http://' + url)

    locless_schemes = ['mailto', 'news', 'file', 'bitcoin']
    if netloc == '' and scheme not in locless_schemes:
        # This fails regardless of anything else.
        # Return immediately to save additional processing
        return None

    # Upstream code will accept a URL like javascript://foo because it
    # appears to have a netloc.  Additionally there are plenty of other
    # schemes that do weird things like launch external programs.  To be
    # on the safe side, we whitelist the scheme.
    if scheme not in ('http', 'https', 'ftp', 'mailto', 'file', 'bitcoin'):
        return None

    # Upstream code scans path, parameters, and query for colon characters
    # because
    #
    #    some aliases [for javascript:] will appear to urllib.parse to have
    #    no scheme. On top of that relative links (i.e.: "foo/bar.html")
    #    have no scheme.
    #
    # We already converted an empty scheme to http:// above, so we skip
    # the colon check, which would also forbid a lot of legitimate URLs.

    # Url passes all tests. Return url as-is.
    return urllib.parse.urlunparse((scheme, netloc, path, params, query, fragment))

def url_to_a(url: Text, text: Optional[Text]=None) -> Union[Element, Text]:
    a = markdown.util.etree.Element('a')

    href = sanitize_url(url)
    target_blank = True
    if href is None:
        # Rejected by sanitize_url; render it as plain text.
        return url
    if text is None:
        text = markdown.util.AtomicString(url)

    href = rewrite_if_relative_link(href)
    target_blank = href[:1] != '#' and 'mailto:' not in href[:7]

    a.set('href', href)
    a.text = text
    fixup_link(a, target_blank)
    return a

class VerbosePattern(markdown.inlinepatterns.Pattern):
    def __init__(self, pattern: Text) -> None:
        markdown.inlinepatterns.Pattern.__init__(self, ' ')

        # HACK: we just had python-markdown compile an empty regex.
        # Now replace with the real regex compiled with the flags we want.

        self.pattern = pattern
        self.compiled_re = re.compile("^(.*?)%s(.*?)$" % pattern,
                                      re.DOTALL | re.UNICODE | re.VERBOSE)

class AutoLink(VerbosePattern):
    def handleMatch(self, match: Match[Text]) -> ElementStringNone:
        url = match.group('url')
        return url_to_a(url)

class UListProcessor(markdown.blockprocessors.UListProcessor):
    """ Process unordered list blocks.

        Based on markdown.blockprocessors.UListProcessor, but does not accept
        '+' or '-' as a bullet character."""

    TAG = 'ul'
    RE = re.compile('^[ ]{0,3}[*][ ]+(.*)')

    def __init__(self, parser: Any) -> None:

        # HACK: Set the tab length to 2 just for the initialization of
        # this class, so that bulleted lists (and only bulleted lists)
        # work off 2-space indentation.
        parser.markdown.tab_length = 2
        super().__init__(parser)
        parser.markdown.tab_length = 4

class ListIndentProcessor(markdown.blockprocessors.ListIndentProcessor):
    """ Process unordered list blocks.

        Based on markdown.blockprocessors.ListIndentProcessor, but with 2-space indent
    """

    def __init__(self, parser: Any) -> None:

        # HACK: Set the tab length to 2 just for the initialization of
        # this class, so that bulleted lists (and only bulleted lists)
        # work off 2-space indentation.
        parser.markdown.tab_length = 2
        super().__init__(parser)
        parser.markdown.tab_length = 4

class BugdownUListPreprocessor(markdown.preprocessors.Preprocessor):
    """ Allows unordered list blocks that come directly after a
        paragraph to be rendered as an unordered list

        Detects paragraphs that have a matching list item that comes
        directly after a line of text, and inserts a newline between
        to satisfy Markdown"""

    LI_RE = re.compile('^[ ]{0,3}[*][ ]+(.*)', re.MULTILINE)
    HANGING_ULIST_RE = re.compile('^.+\\n([ ]{0,3}[*][ ]+.*)', re.MULTILINE)

    def run(self, lines: List[Text]) -> List[Text]:
        """ Insert a newline between a paragraph and ulist if missing """
        inserts = 0
        fence = None
        copy = lines[:]
        for i in range(len(lines) - 1):
            # Ignore anything that is inside a fenced code block
            m = FENCE_RE.match(lines[i])
            if not fence and m:
                fence = m.group('fence')
            elif fence and m and fence == m.group('fence'):
                fence = None

            # If we're not in a fenced block and we detect an upcoming list
            #  hanging off a paragraph, add a newline
            if (not fence and lines[i] and
                self.LI_RE.match(lines[i+1]) and
                    not self.LI_RE.match(lines[i])):

                copy.insert(i+inserts+1, '')
                inserts += 1
        return copy

class AutoNumberOListPreprocessor(markdown.preprocessors.Preprocessor):
    """ Finds a sequence of lines numbered by the same number"""
    RE = re.compile(r'^([ ]*)(\d+)\.[ ]+(.*)')
    TAB_LENGTH = 2

    def run(self, lines):
        # type: (List[Text]) -> List[Text]
        new_lines = []  # type: List[Text]
        current_list = []  # type: List[Match[Text]]
        current_indent = 0

        for line in lines:
            m = self.RE.match(line)

            # Remember if this line is a continuation of already started list
            is_next_item = (m and current_list
                            and current_indent == len(m.group(1)) // self.TAB_LENGTH)

            if not is_next_item:
                # There is no more items in the list we were processing
                new_lines.extend(self.renumber(current_list))
                current_list = []

            if not m:
                # Ordinary line
                new_lines.append(line)
            elif is_next_item:
                # Another list item
                current_list.append(m)
            else:
                # First list item
                current_list = [m]
                current_indent = len(m.group(1)) // self.TAB_LENGTH

        new_lines.extend(self.renumber(current_list))

        return new_lines

    def renumber(self, mlist):
        # type: (List[Match[Text]]) -> List[Text]
        if not mlist:
            return []

        start_number = int(mlist[0].group(2))

        # Change numbers only if every one is the same
        change_numbers = True
        for m in mlist:
            if int(m.group(2)) != start_number:
                change_numbers = False
                break

        lines = []  # type: List[Text]
        counter = start_number

        for m in mlist:
            number = str(counter) if change_numbers else m.group(2)
            lines.append('%s%s. %s' % (m.group(1), number, m.group(3)))
            counter += 1

        return lines

# Based on markdown.inlinepatterns.LinkPattern
class LinkPattern(markdown.inlinepatterns.Pattern):
    """ Return a link element from the given match. """

    def handleMatch(self, m: Match[Text]) -> Optional[Element]:
        href = m.group(9)
        if not href:
            return None

        if href[0] == "<":
            href = href[1:-1]
        href = sanitize_url(self.unescape(href.strip()))
        if href is None:
            return None

        href = rewrite_if_relative_link(href)

        el = markdown.util.etree.Element('a')
        el.text = m.group(2)
        el.set('href', href)
        fixup_link(el, target_blank=(href[:1] != '#'))
        return el

def prepare_realm_pattern(source: Text) -> Text:
    """ Augment a realm filter so it only matches after start-of-string,
    whitespace, or opening delimiters, won't match if there are word
    characters directly after, and saves what was matched as "name". """
    return r"""(?<![^\s'"\(,:<])(?P<name>""" + source + ')(?!\w)'

# Given a regular expression pattern, linkifies groups that match it
# using the provided format string to construct the URL.
class RealmFilterPattern(markdown.inlinepatterns.Pattern):
    """ Applied a given realm filter to the input """

    def __init__(self, source_pattern: Text,
                 format_string: Text,
                 markdown_instance: Optional[markdown.Markdown]=None) -> None:
        self.pattern = prepare_realm_pattern(source_pattern)
        self.format_string = format_string
        markdown.inlinepatterns.Pattern.__init__(self, self.pattern, markdown_instance)

    def handleMatch(self, m: Match[Text]) -> Union[Element, Text]:
        return url_to_a(self.format_string % m.groupdict(),
                        m.group("name"))

class UserMentionPattern(markdown.inlinepatterns.Pattern):
    def handleMatch(self, m: Match[Text]) -> Optional[Element]:
        match = m.group(2)

        if current_message and db_data is not None:
            if match.startswith("**") and match.endswith("**"):
                name = match[2:-2]
            else:
                return None

            wildcard = mention.user_mention_matches_wildcard(name)
            user = db_data['mention_data'].get_user(name)

            if wildcard:
                current_message.mentions_wildcard = True
                email = '*'
                user_id = "*"
            elif user:
                current_message.mentions_user_ids.add(user['id'])
                email = user['email']
                name = user['full_name']
                user_id = str(user['id'])
            else:
                # Don't highlight @mentions that don't refer to a valid user
                return None

            el = markdown.util.etree.Element("span")
            el.set('class', 'user-mention')
            el.set('data-user-email', email)
            el.set('data-user-id', user_id)
            el.text = "@%s" % (name,)
            return el
        return None

class UserGroupMentionPattern(markdown.inlinepatterns.Pattern):
    def handleMatch(self, m: Match[Text]) -> Optional[Element]:
        match = m.group(2)

        if current_message and db_data is not None:
            name = extract_user_group(match)
            user_group = db_data['mention_data'].get_user_group(name)
            if user_group:
                current_message.mentions_user_group_ids.add(user_group.id)
                name = user_group.name
                user_group_id = str(user_group.id)
            else:
                # Don't highlight @-mentions that don't refer to a valid user
                # group.
                return None

            el = markdown.util.etree.Element("span")
            el.set('class', 'user-group-mention')
            el.set('data-user-group-id', user_group_id)
            el.text = "@%s" % (name,)
            return el
        return None

class StreamPattern(VerbosePattern):
    def find_stream_by_name(self, name: Match[Text]) -> Optional[Dict[str, Any]]:
        if db_data is None:
            return None
        stream = db_data['stream_names'].get(name)
        return stream

    def handleMatch(self, m: Match[Text]) -> Optional[Element]:
        name = m.group('stream_name')

        if current_message:
            stream = self.find_stream_by_name(name)
            if stream is None:
                return None
            el = markdown.util.etree.Element('a')
            el.set('class', 'stream')
            el.set('data-stream-id', str(stream['id']))
            # TODO: We should quite possibly not be specifying the
            # href here and instead having the browser auto-add the
            # href when it processes a message with one of these, to
            # provide more clarity to API clients.
            el.set('href', '/#narrow/stream/{stream_name}'.format(
                stream_name=urllib.parse.quote(name)))
            el.text = '#{stream_name}'.format(stream_name=name)
            return el
        return None

def possible_linked_stream_names(content: Text) -> Set[Text]:
    matches = re.findall(STREAM_LINK_REGEX, content, re.VERBOSE)
    return set(matches)

class AlertWordsNotificationProcessor(markdown.preprocessors.Preprocessor):
    def run(self, lines: Iterable[Text]) -> Iterable[Text]:
        if current_message and db_data is not None:
            # We check for alert words here, the set of which are
            # dependent on which users may see this message.
            #
            # Our caller passes in the list of possible_words.  We
            # don't do any special rendering; we just append the alert words
            # we find to the set current_message.alert_words.

            realm_words = db_data['possible_words']

            content = '\n'.join(lines).lower()

            allowed_before_punctuation = "|".join([r'\s', '^', r'[\(\".,\';\[\*`>]'])
            allowed_after_punctuation = "|".join([r'\s', '$', r'[\)\"\?:.,\';\]!\*`]'])

            for word in realm_words:
                escaped = re.escape(word.lower())
                match_re = re.compile('(?:%s)%s(?:%s)' %
                                      (allowed_before_punctuation,
                                       escaped,
                                       allowed_after_punctuation))
                if re.search(match_re, content):
                    current_message.alert_words.add(word)

        return lines

# This prevents realm_filters from running on the content of a
# Markdown link, breaking up the link.  This is a monkey-patch, but it
# might be worth sending a version of this change upstream.
class AtomicLinkPattern(LinkPattern):
    def handleMatch(self, m: Match[Text]) -> Optional[Element]:
        ret = LinkPattern.handleMatch(self, m)
        if ret is None:
            return None
        if not isinstance(ret, str):
            ret.text = markdown.util.AtomicString(ret.text)
        return ret

# These are used as keys ("realm_filters_keys") to md_engines and the respective
# realm filter caches
DEFAULT_BUGDOWN_KEY = -1
ZEPHYR_MIRROR_BUGDOWN_KEY = -2

class Bugdown(markdown.Extension):
    def __init__(self, *args: Any, **kwargs: Union[bool, int, List[Any]]) -> None:
        # define default configs
        self.config = {
            "realm_filters": [kwargs['realm_filters'],
                              "Realm-specific filters for realm_filters_key %s" % (kwargs['realm'],)],
            "realm": [kwargs['realm'], "Realm id"],
            "code_block_processor_disabled": [kwargs['code_block_processor_disabled'],
                                              "Disabled for email gateway"]
        }

        super().__init__(*args, **kwargs)

    def extendMarkdown(self, md: markdown.Markdown, md_globals: Dict[str, Any]) -> None:
        del md.preprocessors['reference']

        if self.getConfig('code_block_processor_disabled'):
            del md.parser.blockprocessors['code']

        for k in ('image_link', 'image_reference', 'automail',
                  'autolink', 'link', 'reference', 'short_reference',
                  'escape', 'strong_em', 'emphasis', 'emphasis2',
                  'linebreak', 'strong', 'backtick'):
            del md.inlinePatterns[k]
        try:
            # linebreak2 was removed upstream in version 3.2.1, so
            # don't throw an error if it is not there
            del md.inlinePatterns['linebreak2']
        except Exception:
            pass

        md.preprocessors.add("custom_text_notifications", AlertWordsNotificationProcessor(md), "_end")

        # Inline code block without whitespace stripping
        md.inlinePatterns.add(
            "backtick",
            BacktickPattern(r'(?:(?<!\\)((?:\\{2})+)(?=`+)|(?<!\\)(`+)(.+?)(?<!`)\3(?!`))'),
            "_begin")

        # Custom bold syntax: **foo** but not __foo__
        md.inlinePatterns.add('strong',
                              markdown.inlinepatterns.SimpleTagPattern(r'(\*\*)([^\n]+?)\2', 'strong'),
                              '>not_strong')

        # Custom strikethrough syntax: ~~foo~~
        md.inlinePatterns.add('del',
                              markdown.inlinepatterns.SimpleTagPattern(
                                  r'(?<!~)(\~\~)([^~\n]+?)(\~\~)(?!~)', 'del'), '>strong')

        # Text inside ** must start and end with a word character
        # it need for things like "const char *x = (char *)y"
        md.inlinePatterns.add(
            'emphasis',
            markdown.inlinepatterns.SimpleTagPattern(r'(\*)(?!\s+)([^\*^\n]+)(?<!\s)\*', 'em'),
            '>strong')

        for k in ('hashheader', 'setextheader', 'olist', 'ulist', 'indent'):
            del md.parser.blockprocessors[k]

        md.parser.blockprocessors.add('ulist', UListProcessor(md.parser), '>hr')
        md.parser.blockprocessors.add('indent', ListIndentProcessor(md.parser), '<ulist')

        # Original regex for blockquote is RE = re.compile(r'(^|\n)[ ]{0,3}>[ ]?(.*)')
        md.parser.blockprocessors['quote'].RE = re.compile(
            r'(^|\n)(?!(?:[ ]{0,3}>\s*(?:$|\n))*(?:$|\n))'
            r'[ ]{0,3}>[ ]?(.*)')

        # Note that !gravatar syntax should be deprecated long term.
        md.inlinePatterns.add('avatar', Avatar(AVATAR_REGEX), '>backtick')
        md.inlinePatterns.add('gravatar', Avatar(GRAVATAR_REGEX), '>backtick')

        md.inlinePatterns.add(
            'stream_subscribe_button',
            StreamSubscribeButton(
                r'!_stream_subscribe_button\((?P<stream_name>(?:[^)\\]|\\\)|\\)*)\)'),
            '>backtick')
        md.inlinePatterns.add(
            'modal_link',
            ModalLink(r'!modal_link\((?P<relative_url>[^)]*), (?P<text>[^)]*)\)'),
            '>avatar')
        md.inlinePatterns.add('usermention', UserMentionPattern(mention.find_mentions), '>backtick')
        md.inlinePatterns.add('usergroupmention',
                              UserGroupMentionPattern(mention.user_group_mentions),
                              '>backtick')
        md.inlinePatterns.add('stream', StreamPattern(STREAM_LINK_REGEX), '>backtick')
        md.inlinePatterns.add(
            'tex',
            Tex(r'\B(?<!\$)\$\$(?P<body>[^\n_$](\\\$|[^$\n])*)\$\$(?!\$)\B'),
            '>backtick')
        md.inlinePatterns.add('emoji', Emoji(EMOJI_REGEX), '_end')
        md.inlinePatterns.add('unicodeemoji', UnicodeEmoji(unicode_emoji_regex), '_end')
        md.inlinePatterns.add('link', AtomicLinkPattern(markdown.inlinepatterns.LINK_RE, md), '>avatar')

        for (pattern, format_string, id) in self.getConfig("realm_filters"):
            md.inlinePatterns.add('realm_filters/%s' % (pattern,),
                                  RealmFilterPattern(pattern, format_string), '>link')

        # A link starts at a word boundary, and ends at space, punctuation, or end-of-input.
        #
        # We detect a url either by the `https?://` or by building around the TLD.

        # In lieu of having a recursive regex (which python doesn't support) to match
        # arbitrary numbers of nested matching parenthesis, we manually build a regexp that
        # can match up to six
        # The inner_paren_contents chunk matches the innermore non-parenthesis-holding text,
        # and the paren_group matches text with, optionally, a matching set of parens
        inner_paren_contents = r"[^\s()\"]*"
        paren_group = r"""
                        [^\s()\"]*?            # Containing characters that won't end the URL
                        (?: \( %s \)           # and more characters in matched parens
                            [^\s()\"]*?        # followed by more characters
                        )*                     # zero-or-more sets of paired parens
                       """
        nested_paren_chunk = paren_group
        for i in range(6):
            nested_paren_chunk = nested_paren_chunk % (paren_group,)
        nested_paren_chunk = nested_paren_chunk % (inner_paren_contents,)
        tlds = '|'.join(list_of_tlds())
        link_regex = r"""
            (?<![^\s'"\(,:<])    # Start after whitespace or specified chars
                                 # (Double-negative lookbehind to allow start-of-string)
            (?P<url>             # Main group
                (?:(?:           # Domain part
                    https?://[\w.:@-]+?   # If it has a protocol, anything goes.
                   |(?:                   # Or, if not, be more strict to avoid false-positives
                        (?:[\w-]+\.)+     # One or more domain components, separated by dots
                        (?:%s)            # TLDs (filled in via format from tlds-alpha-by-domain.txt)
                    )
                )
                (?:/             # A path, beginning with /
                    %s           # zero-to-6 sets of paired parens
                )?)              # Path is optional
                | (?:[\w.-]+\@[\w.-]+\.[\w]+) # Email is separate, since it can't have a path
                %s               # File path start with file:///, enable by setting ENABLE_FILE_LINKS=True
                | (?:bitcoin:[13][a-km-zA-HJ-NP-Z1-9]{25,34})  # Bitcoin address pattern, see https://mokagio.github.io/tech-journal/2014/11/21/regex-bitcoin.html
            )
            (?=                            # URL must be followed by (not included in group)
                [!:;\?\),\.\'\"\>]*         # Optional punctuation characters
                (?:\Z|\s)                  # followed by whitespace or end of string
            )
            """ % (tlds, nested_paren_chunk,
                   r"| (?:file://(/[^/ ]*)+/?)" if settings.ENABLE_FILE_LINKS else r"")
        md.inlinePatterns.add('autolink', AutoLink(link_regex), '>link')

        md.preprocessors.add('hanging_ulists',
                             BugdownUListPreprocessor(md),
                             "_begin")

        md.preprocessors.add('auto_number_olist',
                             AutoNumberOListPreprocessor(md),
                             "_begin")

        md.treeprocessors.add("inline_interesting_links", InlineInterestingLinkProcessor(md, self), "_end")

        if settings.CAMO_URI:
            md.treeprocessors.add("rewrite_to_https", InlineHttpsProcessor(md), "_end")

        if self.getConfig("realm") == ZEPHYR_MIRROR_BUGDOWN_KEY:
            # Disable almost all inline patterns for zephyr mirror
            # users' traffic that is mirrored.  Note that
            # inline_interesting_links is a treeprocessor and thus is
            # not removed
            for k in list(md.inlinePatterns.keys()):
                if k not in ["autolink"]:
                    del md.inlinePatterns[k]
            for k in list(md.treeprocessors.keys()):
                if k not in ["inline_interesting_links", "inline", "rewrite_to_https"]:
                    del md.treeprocessors[k]
            for k in list(md.preprocessors.keys()):
                if k not in ["custom_text_notifications"]:
                    del md.preprocessors[k]
            for k in list(md.parser.blockprocessors.keys()):
                if k not in ["paragraph"]:
                    del md.parser.blockprocessors[k]

md_engines = {}  # type: Dict[Tuple[int, bool], markdown.Markdown]
realm_filter_data = {}  # type: Dict[int, List[Tuple[Text, Text, int]]]

class EscapeHtml(markdown.Extension):
    def extendMarkdown(self, md: markdown.Markdown, md_globals: Dict[str, Any]) -> None:
        del md.preprocessors['html_block']
        del md.inlinePatterns['html']

def make_md_engine(realm_filters_key: int, email_gateway: bool) -> None:
    md_engine_key = (realm_filters_key, email_gateway)
    if md_engine_key in md_engines:
        del md_engines[md_engine_key]

    realm_filters = realm_filter_data[realm_filters_key]
    md_engines[md_engine_key] = markdown.Markdown(
        output_format = 'html',
        extensions    = [
            'markdown.extensions.nl2br',
            'markdown.extensions.tables',
            codehilite.makeExtension(
                linenums=False,
                guess_lang=False
            ),
            fenced_code.makeExtension(),
            EscapeHtml(),
            Bugdown(realm_filters=realm_filters,
                    realm=realm_filters_key,
                    code_block_processor_disabled=email_gateway)])

def subject_links(realm_filters_key: int, subject: Text) -> List[Text]:
    matches = []  # type: List[Text]

    realm_filters = realm_filters_for_realm(realm_filters_key)

    for realm_filter in realm_filters:
        pattern = prepare_realm_pattern(realm_filter[0])
        for m in re.finditer(pattern, subject):
            matches += [realm_filter[1] % m.groupdict()]
    return matches

def maybe_update_markdown_engines(realm_filters_key: Optional[int], email_gateway: bool) -> None:
    # If realm_filters_key is None, load all filters
    global realm_filter_data
    if realm_filters_key is None:
        all_filters = all_realm_filters()
        all_filters[DEFAULT_BUGDOWN_KEY] = []
        for realm_filters_key, filters in all_filters.items():
            realm_filter_data[realm_filters_key] = filters
            make_md_engine(realm_filters_key, email_gateway)
        # Hack to ensure that getConfig("realm") is right for mirrored Zephyrs
        realm_filter_data[ZEPHYR_MIRROR_BUGDOWN_KEY] = []
        make_md_engine(ZEPHYR_MIRROR_BUGDOWN_KEY, False)
    else:
        realm_filters = realm_filters_for_realm(realm_filters_key)
        if realm_filters_key not in realm_filter_data or    \
                realm_filter_data[realm_filters_key] != realm_filters:
            # Realm filters data has changed, update `realm_filter_data` and any
            # of the existing markdown engines using this set of realm filters.
            realm_filter_data[realm_filters_key] = realm_filters
            for email_gateway_flag in [True, False]:
                if (realm_filters_key, email_gateway_flag) in md_engines:
                    # Update only existing engines(if any), don't create new one.
                    make_md_engine(realm_filters_key, email_gateway_flag)

        if (realm_filters_key, email_gateway) not in md_engines:
            # Markdown engine corresponding to this key doesn't exists so create one.
            make_md_engine(realm_filters_key, email_gateway)

# We want to log Markdown parser failures, but shouldn't log the actual input
# message for privacy reasons.  The compromise is to replace all alphanumeric
# characters with 'x'.
#
# We also use repr() to improve reproducibility, and to escape terminal control
# codes, which can do surprisingly nasty things.
_privacy_re = re.compile('\\w', flags=re.UNICODE)
def privacy_clean_markdown(content: Text) -> Text:
    return repr(_privacy_re.sub('x', content))


# Filters such as UserMentionPattern need a message, but python-markdown
# provides no way to pass extra params through to a pattern. Thus, a global.
current_message = None  # type: Optional[Message]

# We avoid doing DB queries in our markdown thread to avoid the overhead of
# opening a new DB connection. These connections tend to live longer than the
# threads themselves, as well.
db_data = None  # type: Optional[Dict[Text, Any]]

def log_bugdown_error(msg: str) -> None:
    """We use this unusual logging approach to log the bugdown error, in
    order to prevent AdminNotifyHandler from sending the santized
    original markdown formatting into another Zulip message, which
    could cause an infinite exception loop."""
    logging.getLogger('').error(msg)

def get_email_info(realm_id: int, emails: Set[Text]) -> Dict[Text, FullNameInfo]:
    if not emails:
        return dict()

    q_list = {
        Q(email__iexact=email.strip().lower())
        for email in emails
    }

    rows = UserProfile.objects.filter(
        realm_id=realm_id
    ).filter(
        functools.reduce(lambda a, b: a | b, q_list),
    ).values(
        'id',
        'email',
    )

    dct = {
        row['email'].strip().lower(): row
        for row in rows
    }
    return dct

def get_full_name_info(realm_id: int, full_names: Set[Text]) -> Dict[Text, FullNameInfo]:
    if not full_names:
        return dict()

    q_list = {
        Q(full_name__iexact=full_name)
        for full_name in full_names
    }

    rows = UserProfile.objects.filter(
        realm_id=realm_id,
        is_active=True,
    ).filter(
        functools.reduce(lambda a, b: a | b, q_list),
    ).values(
        'id',
        'full_name',
        'email',
    )

    dct = {
        row['full_name'].lower(): row
        for row in rows
    }
    return dct

class MentionData:
    def __init__(self, realm_id: int, content: Text) -> None:
        full_names = possible_mentions(content)
        self.full_name_info = get_full_name_info(realm_id, full_names)
        self.user_ids = {
            row['id']
            for row in self.full_name_info.values()
        }

        user_group_names = possible_user_group_mentions(content)
        self.user_group_name_info = get_user_group_name_info(realm_id, user_group_names)
        group_ids = [group.id for group in self.user_group_name_info.values()]
        membership = UserGroupMembership.objects.filter(user_group_id__in=group_ids)
        self.user_group_members = defaultdict(list)  # type: Dict[int, List[int]]
        for info in membership.values('user_group_id', 'user_profile_id'):
            group_id = info['user_group_id']
            user_profile_id = info['user_profile_id']
            self.user_group_members[group_id].append(user_profile_id)

    def get_user(self, name: Text) -> Optional[FullNameInfo]:
        return self.full_name_info.get(name.lower(), None)

    def get_user_ids(self) -> Set[int]:
        """
        Returns the user IDs that might have been mentioned by this
        content.  Note that because this data structure has not parsed
        the message and does not know about escaping/code blocks, this
        will overestimate the list of user ids.
        """
        return self.user_ids

    def get_user_group(self, name: Text) -> Optional[UserGroup]:
        return self.user_group_name_info.get(name.lower(), None)

    def get_group_members(self, user_group_id: int) -> List[int]:
        return self.user_group_members.get(user_group_id, [])

def get_user_group_name_info(realm_id: int, user_group_names: Set[Text]) -> Dict[Text, UserGroup]:
    if not user_group_names:
        return dict()

    rows = UserGroup.objects.filter(realm_id=realm_id,
                                    name__in=user_group_names)
    dct = {row.name.lower(): row for row in rows}
    return dct

def get_stream_name_info(realm: Realm, stream_names: Set[Text]) -> Dict[Text, FullNameInfo]:
    if not stream_names:
        return dict()

    q_list = {
        Q(name=name)
        for name in stream_names
    }

    rows = get_active_streams(
        realm=realm,
    ).filter(
        functools.reduce(lambda a, b: a | b, q_list),
    ).values(
        'id',
        'name',
    )

    dct = {
        row['name']: row
        for row in rows
    }
    return dct


def do_convert(content: Text,
               message: Optional[Message]=None,
               message_realm: Optional[Realm]=None,
               possible_words: Optional[Set[Text]]=None,
               sent_by_bot: Optional[bool]=False,
               mention_data: Optional[MentionData]=None,
               email_gateway: Optional[bool]=False) -> Text:
    """Convert Markdown to HTML, with Zulip-specific settings and hacks."""
    # This logic is a bit convoluted, but the overall goal is to support a range of use cases:
    # * Nothing is passed in other than content -> just run default options (e.g. for docs)
    # * message is passed, but no realm is -> look up realm from message
    # * message_realm is passed -> use that realm for bugdown purposes
    if message is not None:
        if message_realm is None:
            message_realm = message.get_realm()
    if message_realm is None:
        realm_filters_key = DEFAULT_BUGDOWN_KEY
    else:
        realm_filters_key = message_realm.id

    if (message is not None and message.sender.realm.is_zephyr_mirror_realm and
            message.sending_client.name == "zephyr_mirror"):
        # Use slightly customized Markdown processor for content
        # delivered via zephyr_mirror
        realm_filters_key = ZEPHYR_MIRROR_BUGDOWN_KEY

    maybe_update_markdown_engines(realm_filters_key, email_gateway)
    md_engine_key = (realm_filters_key, email_gateway)

    if md_engine_key in md_engines:
        _md_engine = md_engines[md_engine_key]
    else:
        if DEFAULT_BUGDOWN_KEY not in md_engines:
            maybe_update_markdown_engines(realm_filters_key=None, email_gateway=False)

        _md_engine = md_engines[(DEFAULT_BUGDOWN_KEY, email_gateway)]
    # Reset the parser; otherwise it will get slower over time.
    _md_engine.reset()

    global current_message
    current_message = message

    # Pre-fetch data from the DB that is used in the bugdown thread
    global db_data
    if message is not None:
        assert message_realm is not None  # ensured above if message is not None
        if possible_words is None:
            possible_words = set()  # Set[Text]

        # Here we fetch the data structures needed to render
        # mentions/avatars/stream mentions from the database, but only
        # if there is syntax in the message that might use them, since
        # the fetches are somewhat expensive and these types of syntax
        # are uncommon enough that it's a useful optimization.

        if mention_data is None:
            mention_data = MentionData(message_realm.id, content)

        emails = possible_avatar_emails(content)
        email_info = get_email_info(message_realm.id, emails)

        stream_names = possible_linked_stream_names(content)
        stream_name_info = get_stream_name_info(message_realm, stream_names)

        if content_has_emoji_syntax(content):
            realm_emoji = message_realm.get_emoji()
        else:
            realm_emoji = dict()

        db_data = {
            'possible_words': possible_words,
            'email_info': email_info,
            'mention_data': mention_data,
            'realm_emoji': realm_emoji,
            'realm_uri': message_realm.uri,
            'sent_by_bot': sent_by_bot,
            'stream_names': stream_name_info,
        }

    try:
        # Spend at most 5 seconds rendering.
        # Sometimes Python-Markdown is really slow; see
        # https://trac.zulip.net/ticket/345
        rendered_content = timeout(5, _md_engine.convert, content)

        # Throw an exception if the content is huge; this protects the
        # rest of the codebase from any bugs where we end up rendering
        # something huge.
        if len(rendered_content) > MAX_MESSAGE_LENGTH * 2:
            raise BugdownRenderingException()
        return rendered_content
    except Exception:
        cleaned = privacy_clean_markdown(content)

        # Output error to log as well as sending a zulip and email
        log_bugdown_error('Exception in Markdown parser: %sInput (sanitized) was: %s'
                          % (traceback.format_exc(), cleaned))
        subject = "Markdown parser failure on %s" % (platform.node(),)
        mail.mail_admins(
            subject, "Failed message: %s\n\n%s\n\n" % (cleaned, traceback.format_exc()),
            fail_silently=False)

        raise BugdownRenderingException()
    finally:
        current_message = None
        db_data = None

bugdown_time_start = 0.0
bugdown_total_time = 0.0
bugdown_total_requests = 0

def get_bugdown_time() -> float:
    return bugdown_total_time

def get_bugdown_requests() -> int:
    return bugdown_total_requests

def bugdown_stats_start() -> None:
    global bugdown_time_start
    bugdown_time_start = time.time()

def bugdown_stats_finish() -> None:
    global bugdown_total_time
    global bugdown_total_requests
    global bugdown_time_start
    bugdown_total_requests += 1
    bugdown_total_time += (time.time() - bugdown_time_start)

def convert(content: Text,
            message: Optional[Message]=None,
            message_realm: Optional[Realm]=None,
            possible_words: Optional[Set[Text]]=None,
            sent_by_bot: Optional[bool]=False,
            mention_data: Optional[MentionData]=None,
            email_gateway: Optional[bool]=False) -> Text:
    bugdown_stats_start()
    ret = do_convert(content, message, message_realm,
                     possible_words, sent_by_bot, mention_data, email_gateway)
    bugdown_stats_finish()
    return ret
