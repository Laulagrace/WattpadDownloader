import asyncio
from ebooklib import epub
import unicodedata
import re
from aiohttp_client_cache import FileBackend
from aiohttp_client_cache.session import CachedSession

headers = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36",
}

cache = FileBackend(
    use_temp=True,
    expire_after=43200,  # 12 hours
)


async def wp_get_cookies(username: str, password: str) -> dict:
    """Retrieves authorization cookies from Wattpad by logging in with user creds.

    Args:
        username (str): Username.
        password (str): Password.

    Raises:
        ValueError: Bad status code.
        ValueError: No cookies returned.

    Returns:
        dict: Authorization cookies.
    """
    async with CachedSession(headers=headers) as session:
        async with session.post(
            "https://www.wattpad.com/auth/login?nextUrl=%2F&_data=routes%2Fauth%2Flogin",
            data={
                "username": username.lower(),
                "password": password,
            },  # the username.lower() is for caching
        ) as response:
            if response.status != 204:
                raise ValueError("Not a 204.")
            print(response.cookies, await response.text())
            cookies = {
                k: v.value
                for k, v in response.cookies.items()  # Thanks https://stackoverflow.com/a/32281245
            }

            if not cookies:
                raise ValueError("No cookies.")

            return cookies


async def retrieve_story(story_id: int, retry=True, cookies={}) -> dict:
    """Taking a story_id, return its information from the Wattpad API."""
    async with CachedSession(
        headers=headers,
        cache=cache,
        cookies=cookies,
    ) as session:
        try:
            async with session.get(
                f"https://www.wattpad.com/api/v3/stories/{story_id}?fields=tags,id,title,createDate,modifyDate,language(name),description,completed,mature,url,isPaywalled,user(username),parts(id,title),cover"
            ) as response:
                if not response.ok:
                    if response.status in [404, 400]:
                        return {}
                    raise ValueError("Status Code:", response.status)
                body = await response.json()
        except ValueError:
            if not retry:
                raise asyncio.TimeoutError()
            await asyncio.sleep(15)
            return await retrieve_story(story_id, retry=False)

    return body


async def fetch_part_content(part_id: int, retry: bool = True, cookies={}) -> str:
    """Return the HTML Content of a Part."""
    async with CachedSession(headers=headers, cache=cache, cookies=cookies) as session:
        async with session.get(
            f"https://www.wattpad.com/apiv2/?m=storytext&id={part_id}"
        ) as response:
            if not response.ok:
                if response.status in [404, 400]:
                    return ""
                raise ValueError("Status Code:", response.status)
            body = await response.text()

    return body


async def fetch_cover(url: str, cookies={}) -> bytes:
    """Fetch image bytes."""
    async with CachedSession(headers=headers, cache=cache, cookies=cookies) as session:
        async with session.get(url) as response:
            if not response.ok:
                if response.status in [404, 400]:
                    return bytes()
                raise ValueError("Status Code:", response.status)
            body = await response.read()

    return body


def slugify(value, allow_unicode=False) -> str:
    """
    Taken from https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.

    Thanks https://stackoverflow.com/a/295466.
    """
    value = str(value)
    if allow_unicode:
        value = unicodedata.normalize("NFKC", value)
    else:
        value = (
            unicodedata.normalize("NFKD", value)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-_")


# --- #


def set_metadata(book, data):
    book.add_author(data["user"]["username"])

    book.add_metadata("DC", "description", data["description"])
    book.add_metadata("DC", "created", data["createDate"])
    book.add_metadata("DC", "modified", data["modifyDate"])
    book.add_metadata("DC", "language", data["language"]["name"])

    book.add_metadata(
        None, "meta", "", {"name": "tags", "content": ", ".join(data["tags"])}
    )
    book.add_metadata(
        None, "meta", "", {"name": "mature", "content": str(int(data["mature"]))}
    )
    book.add_metadata(
        None, "meta", "", {"name": "completed", "content": str(int(data["completed"]))}
    )


async def set_cover(book, data):
    book.set_cover("cover.jpg", await fetch_cover(data["cover"]))


async def add_chapters(book, data, cookies={}):
    chapters = []

    for part in data["parts"]:
        content = await fetch_part_content(part["id"], cookies=cookies)
        title = part["title"]

        # Thanks https://eu17.proxysite.com/process.php?d=5VyWYcoQl%2BVF0BYOuOavtvjOloFUZz2BJ%2Fepiusk6Nz7PV%2B9i8rs7cFviGftrBNll%2B0a3qO7UiDkTt4qwCa0fDES&b=1
        chapter = epub.EpubHtml(
            title=title,
            file_name=f"{slugify(title)}.xhtml",
            lang=data["language"]["name"],
        )
        chapter.set_content(f"<h1>{title}</h1>" + content)

        chapters.append(chapter)

        yield title  # Yield the chapter's title upon insertion preceeded by retrieval.

    for chapter in chapters:
        book.add_item(chapter)

    book.toc = tuple(chapters)

    # Thanks https://github.com/aerkalov/ebooklib/blob/master/samples/09_create_image/create.py
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # create spine
    book.spine = ["nav"] + chapters
