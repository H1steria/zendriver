from __future__ import annotations

import asyncio
import base64
import datetime
import logging
import pathlib
import re
import secrets
import typing
import urllib.parse
import warnings
import webbrowser
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Tuple, Union

from .intercept import BaseFetchInterception
from .. import cdp
from . import element, util
from .config import PathLike
from .connection import Connection, ProtocolException
from .expect import DownloadExpectation, RequestExpectation, ResponseExpectation
from ..cdp.fetch import RequestStage
from ..cdp.network import ResourceType


if TYPE_CHECKING:
    from .browser import Browser
    from .element import Element

logger = logging.getLogger(__name__)


class Tab(Connection):
    """
    :ref:`tab` is the controlling mechanism/connection to a 'target',
    for most of us 'target' can be read as 'tab'. however it could also
    be an iframe, serviceworker or background script for example,
    although there isn't much to control for those.

    if you open a new window by using :py:meth:`browser.get(..., new_window=True)`
    your url will open a new window. this window is a 'tab'.
    When you browse to another page, the tab will be the same (it is an browser view).

    So it's important to keep some reference to tab objects, in case you're
    done interacting with elements and want to operate on the page level again.

    Custom CDP commands
    ---------------------------
    Tab object provide many useful and often-used methods. It is also
    possible to utilize the included cdp classes to to something totally custom.

    the cdp package is a set of so-called "domains" with each having methods, events and types.
    to send a cdp method, for example :py:obj:`cdp.page.navigate`, you'll have to check
    whether the method accepts any parameters and whether they are required or not.

    you can use

    ```python
    await tab.send(cdp.page.navigate(url='https://yoururlhere'))
    ```

    so tab.send() accepts a generator object, which is created by calling a cdp method.
    this way you can build very detailed and customized commands.
    (note: finding correct command combo's can be a time consuming task, luckily i added a whole bunch
    of useful methods, preferably having the same api's or lookalikes, as in selenium)


    some useful, often needed and simply required methods
    ===================================================================


    :py:meth:`~find`  |  find(text)
    ----------------------------------------
    find and returns a single element by text match. by default returns the first element found.
    much more powerful is the best_match flag, although also much more expensive.
    when no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    :py:meth:`~find` |  find(text, best_match=True) or find(text, True)
    ---------------------------------------------------------------------------------
    Much more powerful (and expensive!!) than the above, is the use of the `find(text, best_match=True)` flag.
    It will still return 1 element, but when multiple matches are found, picks the one having the
    most similar text length.
    How would that help?
    For example, you search for "login", you'd probably want the "login" button element,
    and not thousands of scripts,meta,headings which happens to contain a string of "login".

    when no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    :py:meth:`~select` | select(selector)
    ----------------------------------------
    find and returns a single element by css selector match.
    when no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    :py:meth:`~select_all` | select_all(selector)
    ------------------------------------------------
    find and returns all elements by css selector match.
    when no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    await :py:obj:`Tab`
    ---------------------------
    calling `await tab` will do a lot of stuff under the hood, and ensures all references
    are up to date. also it allows for the script to "breathe", as it is oftentime faster than your browser or
    webpage. So whenever you get stuck and things crashes or element could not be found, you should probably let
    it "breathe"  by calling `await page`  and/or `await page.sleep()`

    also, it's ensuring :py:obj:`~url` will be updated to the most recent one, which is quite important in some
    other methods.

    Using other and custom CDP commands
    ======================================================
    using the included cdp module, you can easily craft commands, which will always return an generator object.
    this generator object can be easily sent to the :py:meth:`~send`  method.

    :py:meth:`~send`
    ---------------------------
    this is probably THE most important method, although you won't ever call it, unless you want to
    go really custom. the send method accepts a :py:obj:`cdp` command. Each of which can be found in the
    cdp section.

    when you import * from this package, cdp will be in your namespace, and contains all domains/actions/events
    you can act upon.
    """

    browser: Browser | None

    def __init__(
        self,
        websocket_url: str,
        target: cdp.target.TargetInfo,
        browser: Browser | None = None,
        **kwargs: dict[str, typing.Any],
    ):
        super().__init__(websocket_url, target, browser, **kwargs)
        self.browser = browser
        self._dom = None
        self._window_id = None

    @property
    def inspector_url(self) -> str:
        """
        get the inspector url. this url can be used in another browser to show you the devtools interface for
        current tab. useful for debugging (and headless)
        :return:
        :rtype:
        """
        if not self.browser:
            raise ValueError(
                "this tab has no browser attribute, so you can't use inspector_url"
            )

        return f"http://{self.browser.config.host}:{self.browser.config.port}/devtools/inspector.html?ws={self.websocket_url[5:]}"

    def inspector_open(self) -> None:
        webbrowser.open(self.inspector_url, new=2)

    async def disable_dom_agent(self) -> None:
        # The DOM.disable can throw an exception if not enabled,
        # but if it's already disabled, that's not a "real" error.

        # DOM agent hasn't been enabled
        # command:DOM.disable
        # params:[] [code: -32000]

        # If not ignored, an exception is thrown, and masks other problems
        # (e.g., Could not find node with given id)

        try:
            await self.send(cdp.dom.disable())
        except ProtocolException:
            logger.debug("Ignoring DOM.disable exception", exc_info=True)
            pass

    async def open_external_inspector(self) -> None:
        """
        opens the system's browser containing the devtools inspector page
        for this tab. could be handy, especially to debug in headless mode.
        """
        import webbrowser

        webbrowser.open(self.inspector_url)

    async def find(
        self,
        text: str,
        best_match: bool = True,
        return_enclosing_element: bool = True,
        timeout: Union[int, float] = 10,
    ) -> Element:
        """
        find single element by text
        can also be used to wait for such element to appear.

        :param text: text to search for. note: script contents are also considered text
        :type text: str
        :param best_match:  :param best_match:  when True (default), it will return the element which has the most
                                               comparable string length. this could help tremendously, when for example
                                               you search for "login", you'd probably want the login button element,
                                               and not thousands of scripts,meta,headings containing a string of "login".
                                               When False, it will return naively just the first match (but is way faster).
         :type best_match: bool
         :param return_enclosing_element:
                 since we deal with nodes instead of elements, the find function most often returns
                 so called text nodes, which is actually a element of plain text, which is
                 the somehow imaginary "child" of a "span", "p", "script" or any other elements which have text between their opening
                 and closing tags.
                 most often when we search by text, we actually aim for the element containing the text instead of
                 a lousy plain text node, so by default the containing element is returned.

                 however, there are (why not) exceptions, for example elements that use the "placeholder=" property.
                 this text is rendered, but is not a pure text node. in that case you can set this flag to False.
                 since in this case we are probably interested in just that element, and not it's parent.


                 # todo, automatically determine node type
                 # ignore the return_enclosing_element flag if the found node is NOT a text node but a
                 # regular element (one having a tag) in which case that is exactly what we need.
         :type return_enclosing_element: bool
        :param timeout: raise timeout exception when after this many seconds nothing is found.
        :type timeout: float,int
        """
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        text = text.strip()

        while True:
            item = await self.find_element_by_text(
                text, best_match, return_enclosing_element
            )
            if item:
                return item

            if loop.time() - start_time > timeout:
                raise asyncio.TimeoutError(
                    f"Timeout ({timeout}s) waiting for element with text: '{text}'"
                )

            await self.sleep(0.5)

    async def select(
        self,
        selector: str,
        timeout: Union[int, float] = 10,
    ) -> Element:
        """
        find single element by css selector.
        can also be used to wait for such element to appear.

        :param selector: css selector, eg a[href], button[class*=close], a > img[src]
        :type selector: str

        :param timeout: raise timeout exception when after this many seconds nothing is found.
        :type timeout: float,int

        """
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        selector = selector.strip()

        while True:
            item = await self.query_selector(selector)
            if isinstance(item, list):
                if item:
                    return item[0]
            elif item:
                return item

            if loop.time() - start_time > timeout:
                raise asyncio.TimeoutError(
                    f"Timeout ({timeout}s) waiting for element with selector: '{selector}'"
                )

            await self.sleep(0.5)

    async def find_all(
        self,
        text: str,
        timeout: Union[int, float] = 10,
    ) -> List[Element]:
        """
        find multiple elements by text
        can also be used to wait for such element to appear.

        :param text: text to search for. note: script contents are also considered text
        :type text: str

        :param timeout: raise timeout exception when after this many seconds nothing is found.
        :type timeout: float,int
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        text = text.strip()

        while True:
            items = await self.find_elements_by_text(text)
            if items:
                return items

            if loop.time() - now > timeout:
                raise asyncio.TimeoutError(
                    f"Timeout ({timeout}s) waiting for any element with text: '{text}'"
                )

            await self.sleep(0.5)

    async def select_all(
        self,
        selector: str,
        timeout: Union[int, float] = 10,
        include_frames: bool = False,
    ) -> List[Element]:
        """
        find multiple elements by css selector.
        can also be used to wait for such element to appear.


        :param selector: css selector, eg a[href], button[class*=close], a > img[src]
        :type selector: str
        :param timeout: raise timeout exception when after this many seconds nothing is found.
        :type timeout: float,int
        :param include_frames: whether to include results in iframes.
        :type include_frames: bool
        """

        loop = asyncio.get_running_loop()
        now = loop.time()
        selector = selector.strip()

        while True:
            items = []
            if include_frames:
                frames = await self.query_selector_all("iframe")
                for fr in frames:
                    items.extend(await fr.query_selector_all(selector))

            items.extend(await self.query_selector_all(selector))

            if items:
                return items

            if loop.time() - now > timeout:
                raise asyncio.TimeoutError(
                    f"Timeout ({timeout}s) waiting for any element with selector: '{selector}'"
                )

            await self.sleep(0.5)

    async def xpath(self, xpath: str, timeout: float = 2.5) -> List[Element]:  # noqa
        """
        find elements by xpath string.
        if not immediately found, retries are attempted until :ref:`timeout` is reached (default 2.5 seconds).
        in case nothing is found, it returns an empty list. It will not raise.
        this timeout mechanism helps when relying on some element to appear before continuing your script.


        .. code-block:: python

             # find all the inline scripts (script elements without src attribute)
             await tab.xpath('//script[not(@src)]')

             # or here, more complex, but my personal favorite to case-insensitive text search

             await tab.xpath('//text()[ contains( translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"),"test")]')


        :param xpath:
        :type xpath: str
        :param timeout: 2.5
        :type timeout: float
        :return:List[Element] or []
        :rtype:
        """
        items: List[Element] = []
        try:
            await self.send(cdp.dom.enable(), True)
            items = await self.find_all(xpath, timeout=0)
            if not items:
                loop = asyncio.get_running_loop()
                start_time = loop.time()
                while not items:
                    items = await self.find_all(xpath, timeout=0)
                    await self.sleep(0.1)
                    if loop.time() - start_time > timeout:
                        break
        finally:
            await self.disable_dom_agent()
        return items

    async def get(
        self, url: str = "about:blank", new_tab: bool = False, new_window: bool = False
    ) -> Tab:
        """top level get. utilizes the first tab to retrieve given url.

        convenience function known from selenium.
        this function handles waits/sleeps and detects when DOM events fired, so it's the safest
        way of navigating.

        :param url: the url to navigate to
        :param new_tab: open new tab
        :param new_window:  open new window
        :return: Page
        """
        if not self.browser:
            raise AttributeError(
                "this page/tab has no browser attribute, so you can't use get()"
            )
        if new_window and not new_tab:
            new_tab = True

        if new_tab:
            return await self.browser.get(url, new_tab, new_window)
        else:
            await self.send(cdp.page.navigate(url))
            await self.wait()
            return self

    async def query_selector_all(
        self,
        selector: str,
        _node: cdp.dom.Node | Element | None = None,
    ) -> List[Element]:
        """
        equivalent of javascripts document.querySelectorAll.
        this is considered one of the main methods to use in this package.

        it returns all matching :py:obj:`zendriver.Element` objects.

        :param selector: css selector. (first time? => https://www.w3schools.com/cssref/css_selectors.php )
        :type selector: str
        :param _node: internal use
        :type _node:
        :return:
        :rtype:
        """
        doc: Any
        if not _node:
            doc = await self.send(cdp.dom.get_document(-1, True))
        else:
            doc = _node
            if _node.node_name == "IFRAME":
                doc = _node.content_document
        node_ids = []

        try:
            node_ids = await self.send(
                cdp.dom.query_selector_all(doc.node_id, selector)
            )
        except ProtocolException as e:
            if _node is not None:
                if e.message is not None and "could not find node" in e.message.lower():
                    if getattr(_node, "__last", None):
                        delattr(_node, "__last")
                        return []
                    # if supplied node is not found, the dom has changed since acquiring the element
                    # therefore we need to update our passed node and try again
                    if isinstance(_node, Element):
                        await _node.update()
                    # make sure this isn't turned into infinite loop
                    setattr(_node, "__last", True)
                    return await self.query_selector_all(selector, _node)
            else:
                await self.disable_dom_agent()
                raise
        if not node_ids:
            return []
        items = []

        for nid in node_ids:
            node = util.filter_recurse(doc, lambda n: n.node_id == nid)
            # we pass along the retrieved document tree,
            # to improve performance
            if not node:
                continue
            elem = element.create(node, self, doc)
            items.append(elem)

        return items

    async def query_selector(
        self,
        selector: str,
        _node: Optional[Union[cdp.dom.Node, Element]] = None,
    ) -> Element | None:
        """
        find single element based on css selector string

        :param selector: css selector(s)
        :type selector: str
        :return:
        :rtype:
        """
        selector = selector.strip()

        doc: Any
        if not _node:
            doc = await self.send(cdp.dom.get_document(-1, True))
        else:
            doc = _node
            if _node.node_name == "IFRAME":
                doc = _node.content_document
        node_id = None

        try:
            node_id = await self.send(cdp.dom.query_selector(doc.node_id, selector))

        except ProtocolException as e:
            if _node is not None:
                if e.message is not None and "could not find node" in e.message.lower():
                    if getattr(_node, "__last", None):
                        delattr(_node, "__last")
                        return None
                    # if supplied node is not found, the dom has changed since acquiring the element
                    # therefore we need to update our passed node and try again
                    if isinstance(_node, Element):
                        await _node.update()
                    # make sure this isn't turned into infinite loop
                    setattr(_node, "__last", True)
                    return await self.query_selector(selector, _node)
            elif (
                e.message is not None
                and "could not find node" in e.message.lower()
                and doc
            ):
                return None
            else:
                await self.disable_dom_agent()
                raise
        if not node_id:
            return None
        node = util.filter_recurse(doc, lambda n: n.node_id == node_id)
        if not node:
            return None
        return element.create(node, self, doc)

    async def find_elements_by_text(
        self,
        text: str,
        tag_hint: Optional[str] = None,
    ) -> list[Element]:
        """
        returns element which match the given text.
        please note: this may (or will) also return any other element (like inline scripts),
        which happen to contain that text.

        :param text:
        :type text:
        :param tag_hint: when provided, narrows down search to only elements which match given tag eg: a, div, script, span
        :type tag_hint: str
        :return:
        :rtype:
        """
        text = text.strip()
        doc = await self.send(cdp.dom.get_document(-1, True))
        search_id, nresult = await self.send(cdp.dom.perform_search(text, True))
        if nresult:
            node_ids = await self.send(
                cdp.dom.get_search_results(search_id, 0, nresult)
            )
        else:
            node_ids = []

        await self.send(cdp.dom.discard_search_results(search_id))

        if not node_ids:
            node_ids = []
        items = []
        for nid in node_ids:
            node = util.filter_recurse(doc, lambda n: n.node_id == nid)
            if not node:
                try:
                    node = await self.send(cdp.dom.resolve_node(node_id=nid))  # type: ignore
                except ProtocolException:
                    continue
                if not node:
                    continue
                # remote_object = await self.send(cdp.dom.resolve_node(backend_node_id=node.backend_node_id))
                # node_id = await self.send(cdp.dom.request_node(object_id=remote_object.object_id))
            try:
                elem = element.create(node, self, doc)
            except Exception:
                continue
            if elem.node_type == 3:
                # if found element is a text node (which is plain text, and useless for our purpose),
                # we return the parent element of the node (which is often a tag which can have text between their
                # opening and closing tags (that is most tags, except for example "img" and "video", "br")

                if not elem.parent:
                    # check if parent actually has a parent and update it to be absolutely sure
                    await elem.update()

                items.append(
                    elem.parent or elem
                )  # when it really has no parent, use the text node itself
                continue
            else:
                # just add the element itself
                items.append(elem)

        # since we already fetched the entire doc, including shadow and frames
        # let's also search through the iframes
        iframes = util.filter_recurse_all(doc, lambda node: node.node_name == "IFRAME")
        if iframes:
            iframes_elems = [
                element.create(iframe, self, iframe.content_document)
                for iframe in iframes
            ]
            for iframe_elem in iframes_elems:
                if iframe_elem.content_document:
                    iframe_text_nodes = util.filter_recurse_all(
                        iframe_elem,
                        lambda node: node.node_type == 3  # noqa
                        and text.lower() in node.node_value.lower(),
                    )
                    if iframe_text_nodes:
                        iframe_text_elems = [
                            element.create(text_node.node, self, iframe_elem.tree)
                            for text_node in iframe_text_nodes
                        ]
                        items.extend(
                            text_node.parent
                            for text_node in iframe_text_elems
                            if text_node.parent
                        )
        await self.disable_dom_agent()
        return items or []

    async def find_element_by_text(
        self,
        text: str,
        best_match: Optional[bool] = False,
        return_enclosing_element: Optional[bool] = True,
    ) -> Element | None:
        """
        finds and returns the first element containing <text>, or best match

        :param text:
        :type text:
        :param best_match:  when True, which is MUCH more expensive (thus much slower),
                            will find the closest match based on length.
                            this could help tremendously, when for example you search for "login", you'd probably want the login button element,
                            and not thousands of scripts,meta,headings containing a string of "login".

        :type best_match: bool
        :param return_enclosing_element:
        :type return_enclosing_element:
        :return:
        :rtype:
        """
        items = await self.find_elements_by_text(text)
        try:
            if not items:
                return None
            if best_match:
                closest_by_length = min(
                    items, key=lambda el: abs(len(text) - len(el.text_all))
                )
                elem = closest_by_length or items[0]

                return elem
            else:
                # naively just return the first result
                for elem in items:
                    if elem:
                        return elem
        finally:
            pass

        return None

    async def back(self) -> None:
        """
        history back
        """
        await self.send(cdp.runtime.evaluate("window.history.back()"))

    async def forward(self) -> None:
        """
        history forward
        """
        await self.send(cdp.runtime.evaluate("window.history.forward()"))

    async def reload(
        self,
        ignore_cache: Optional[bool] = True,
        script_to_evaluate_on_load: Optional[str] = None,
    ) -> None:
        """
        Reloads the page

        :param ignore_cache: when set to True (default), it ignores cache, and re-downloads the items
        :type ignore_cache:
        :param script_to_evaluate_on_load: script to run on load. I actually haven't experimented with this one, so no guarantees.
        :type script_to_evaluate_on_load:
        :return:
        :rtype:
        """
        await self.send(
            cdp.page.reload(
                ignore_cache=ignore_cache,
                script_to_evaluate_on_load=script_to_evaluate_on_load,
            ),
        )

    async def evaluate(
        self, expression: str, await_promise: bool = False, return_by_value: bool = True
    ) -> (
        Any
        | None
        | typing.Tuple[cdp.runtime.RemoteObject, cdp.runtime.ExceptionDetails | None]
    ):
        remote_object, errors = await self.send(
            cdp.runtime.evaluate(
                expression=expression,
                user_gesture=True,
                await_promise=await_promise,
                return_by_value=return_by_value,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
        if errors:
            raise ProtocolException(errors)

        if remote_object:
            if return_by_value:
                if remote_object.value:
                    return remote_object.value

        return remote_object, errors

    async def js_dumps(
        self, obj_name: str, return_by_value: Optional[bool] = True
    ) -> (
        Any
        | typing.Tuple[cdp.runtime.RemoteObject, cdp.runtime.ExceptionDetails | None]
    ):
        """
        dump given js object with its properties and values as a dict

        note: complex objects might not be serializable, therefore this method is not a "source of thruth"

        :param obj_name: the js object to dump
        :type obj_name: str

        :param return_by_value: if you want an tuple of cdp objects (returnvalue, errors), set this to False
        :type return_by_value: bool

        example
        ------

        x = await self.js_dumps('window')
        print(x)
            '...{
            'pageYOffset': 0,
            'visualViewport': {},
            'screenX': 10,
            'screenY': 10,
            'outerWidth': 1050,
            'outerHeight': 832,
            'devicePixelRatio': 1,
            'screenLeft': 10,
            'screenTop': 10,
            'styleMedia': {},
            'onsearch': None,
            'isSecureContext': True,
            'trustedTypes': {},
            'performance': {'timeOrigin': 1707823094767.9,
            'timing': {'connectStart': 0,
            'navigationStart': 1707823094768,
            ]...
            '
        """
        js_code_a = (
            """
                           function ___dump(obj, _d = 0) {
                               let _typesA = ['object', 'function'];
                               let _typesB = ['number', 'string', 'boolean'];
                               if (_d == 2) {
                                   console.log('maxdepth reached for ', obj);
                                   return
                               }
                               let tmp = {}
                               for (let k in obj) {
                                   if (obj[k] == window) continue;
                                   let v;
                                   try {
                                       if (obj[k] === null || obj[k] === undefined || obj[k] === NaN) {
                                           console.log('obj[k] is null or undefined or Nan', k, '=>', obj[k])
                                           tmp[k] = obj[k];
                                           continue
                                       }
                                   } catch (e) {
                                       tmp[k] = null;
                                       continue
                                   }


                                   if (_typesB.includes(typeof obj[k])) {
                                       tmp[k] = obj[k]
                                       continue
                                   }

                                   try {
                                       if (typeof obj[k] === 'function') {
                                           tmp[k] = obj[k].toString()
                                           continue
                                       }


                                       if (typeof obj[k] === 'object') {
                                           tmp[k] = ___dump(obj[k], _d + 1);
                                           continue
                                       }


                                   } catch (e) {}

                                   try {
                                       tmp[k] = JSON.stringify(obj[k])
                                       continue
                                   } catch (e) {

                                   }
                                   try {
                                       tmp[k] = obj[k].toString();
                                       continue
                                   } catch (e) {}
                               }
                               return tmp
                           }

                           function ___dumpY(obj) {
                               var objKeys = (obj) => {
                                   var [target, result] = [obj, []];
                                   while (target !== null) {
                                       result = result.concat(Object.getOwnPropertyNames(target));
                                       target = Object.getPrototypeOf(target);
                                   }
                                   return result;
                               }
                               return Object.fromEntries(
                                   objKeys(obj).map(_ => [_, ___dump(obj[_])]))

                           }
                           ___dumpY( %s )
                   """
            % obj_name
        )
        js_code_b = (
            """
            ((obj, visited = new WeakSet()) => {
                 if (visited.has(obj)) {
                     return {}
                 }
                 visited.add(obj)
                 var result = {}, _tmp;
                 for (var i in obj) {
                         try {
                             if (i === 'enabledPlugin' || typeof obj[i] === 'function') {
                                 continue;
                             } else if (typeof obj[i] === 'object') {
                                 _tmp = recurse(obj[i], visited);
                                 if (Object.keys(_tmp).length) {
                                     result[i] = _tmp;
                                 }
                             } else {
                                 result[i] = obj[i];
                             }
                         } catch (error) {
                             // console.error('Error:', error);
                         }
                     }
                return result;
            })(%s)
        """
            % obj_name
        )

        # we're purposely not calling self.evaluate here to prevent infinite loop on certain expressions
        remote_object, exception_details = await self.send(
            cdp.runtime.evaluate(
                js_code_a,
                await_promise=True,
                return_by_value=return_by_value,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
        if exception_details:
            # try second variant

            remote_object, exception_details = await self.send(
                cdp.runtime.evaluate(
                    js_code_b,
                    await_promise=True,
                    return_by_value=return_by_value,
                    allow_unsafe_eval_blocked_by_csp=True,
                )
            )

        if exception_details:
            raise ProtocolException(exception_details)
        if return_by_value and remote_object.value:
            return remote_object.value
        else:
            return remote_object, exception_details

    async def close(self) -> None:
        """
        close the current target (ie: tab,window,page)
        :return:
        :rtype:
        :raises: asyncio.TimeoutError
        :raises: RuntimeError
        """

        if not self.browser or not self.browser.connection:
            raise RuntimeError("Browser not yet started. use await browser.start()")

        future = asyncio.get_running_loop().create_future()
        event_type = cdp.target.TargetDestroyed

        async def close_handler(event: cdp.target.TargetDestroyed) -> None:
            if future.done():
                return

            if self.target and event.target_id == self.target.target_id:
                future.set_result(event)

        self.browser.connection.add_handler(event_type, close_handler)

        if self.target and self.target.target_id:
            await self.send(cdp.target.close_target(target_id=self.target.target_id))

        await asyncio.wait_for(future, 10)
        self.browser.connection.remove_handlers(event_type, close_handler)

    async def get_window(self) -> Tuple[cdp.browser.WindowID, cdp.browser.Bounds]:
        """
        get the window Bounds
        :return:
        :rtype:
        """
        window_id, bounds = await self.send(
            cdp.browser.get_window_for_target(self.target_id)
        )
        return window_id, bounds

    async def get_content(self) -> str:
        """
        gets the current page source content (html)
        :return:
        :rtype:
        """
        doc: cdp.dom.Node = await self.send(cdp.dom.get_document(-1, True))
        return await self.send(
            cdp.dom.get_outer_html(backend_node_id=doc.backend_node_id)
        )

    async def maximize(self) -> None:
        """
        maximize page/tab/window
        """
        return await self.set_window_state(state="maximize")

    async def minimize(self) -> None:
        """
        minimize page/tab/window
        """
        return await self.set_window_state(state="minimize")

    async def fullscreen(self) -> None:
        """
        minimize page/tab/window
        """
        return await self.set_window_state(state="fullscreen")

    async def medimize(self) -> None:
        return await self.set_window_state(state="normal")

    async def set_window_size(
        self, left: int = 0, top: int = 0, width: int = 1280, height: int = 1024
    ) -> None:
        """
        set window size and position

        :param left: pixels from the left of the screen to the window top-left corner
        :type left:
        :param top: pixels from the top of the screen to the window top-left corner
        :type top:
        :param width: width of the window in pixels
        :type width:
        :param height: height of the window in pixels
        :type height:
        :return:
        :rtype:
        """
        return await self.set_window_state(left, top, width, height)

    async def activate(self) -> None:
        """
        active this target (ie: tab,window,page)
        """
        if self.target is None:
            raise ValueError("target is none")
        await self.send(cdp.target.activate_target(self.target.target_id))

    async def bring_to_front(self) -> None:
        """
        alias to self.activate
        """
        await self.activate()

    async def set_window_state(
        self,
        left: int = 0,
        top: int = 0,
        width: int = 1280,
        height: int = 720,
        state: str = "normal",
    ) -> None:
        """
        sets the window size or state.

        for state you can provide the full name like minimized, maximized, normal, fullscreen, or
        something which leads to either of those, like min, mini, mi,  max, ma, maxi, full, fu, no, nor
        in case state is set other than "normal", the left, top, width, and height are ignored.

        :param left:
            desired offset from left, in pixels
        :type left: int

        :param top:
            desired offset from the top, in pixels
        :type top: int

        :param width:
            desired width in pixels
        :type width: int

        :param height:
            desired height in pixels
        :type height: int

        :param state:
            can be one of the following strings:
                - normal
                - fullscreen
                - maximized
                - minimized

        :type state: str

        """
        available_states = ["minimized", "maximized", "fullscreen", "normal"]
        window_id: cdp.browser.WindowID
        bounds: cdp.browser.Bounds
        (window_id, bounds) = await self.get_window()

        for state_name in available_states:
            if all(x in state_name for x in state.lower()):
                break
        else:
            raise NameError(
                "could not determine any of %s from input '%s'"
                % (",".join(available_states), state)
            )
        window_state = getattr(
            cdp.browser.WindowState, state_name.upper(), cdp.browser.WindowState.NORMAL
        )
        if window_state == cdp.browser.WindowState.NORMAL:
            bounds = cdp.browser.Bounds(left, top, width, height, window_state)
        else:
            # min, max, full can only be used when current state == NORMAL
            # therefore we first switch to NORMAL
            await self.set_window_state(state="normal")
            bounds = cdp.browser.Bounds(window_state=window_state)

        await self.send(cdp.browser.set_window_bounds(window_id, bounds=bounds))

    async def scroll_down(self, amount: int = 25, speed: int = 800) -> None:
        """
        scrolls down maybe

        :param amount: number in percentage. 25 is a quarter of page, 50 half, and 1000 is 10x the page
        :param speed: number swipe speed in pixels per second (default: 800).
        :type amount: int
        :type speed: int
        :return:
        :rtype:
        """
        window_id: cdp.browser.WindowID
        bounds: cdp.browser.Bounds
        (window_id, bounds) = await self.get_window()
        height = bounds.height if bounds.height else 0

        await self.send(
            cdp.input_.synthesize_scroll_gesture(
                x=0,
                y=0,
                y_distance=-(height * (amount / 100)),
                y_overscroll=0,
                x_overscroll=0,
                prevent_fling=True,
                repeat_delay_ms=0,
                speed=speed,
            )
        )
        await asyncio.sleep(height * (amount / 100) / speed)

    async def scroll_up(self, amount: int = 25, speed: int = 800) -> None:
        """
        scrolls up maybe

        :param amount: number in percentage. 25 is a quarter of page, 50 half, and 1000 is 10x the page
        :param speed: number swipe speed in pixels per second (default: 800).
        :type amount: int
        :type speed: int

        :return:
        :rtype:
        """
        window_id: cdp.browser.WindowID
        bounds: cdp.browser.Bounds
        (window_id, bounds) = await self.get_window()
        height = bounds.height if bounds.height else 0

        await self.send(
            cdp.input_.synthesize_scroll_gesture(
                x=0,
                y=0,
                y_distance=(height * (amount / 100)),
                x_overscroll=0,
                prevent_fling=True,
                repeat_delay_ms=0,
                speed=speed,
            )
        )
        await asyncio.sleep(height * (amount / 100) / speed)

    async def wait_for(
        self,
        selector: str | None = None,
        text: str | None = None,
        timeout: int | float = 10,
    ) -> Element:
        """
        variant on query_selector_all and find_elements_by_text
        this variant takes either selector or text, and will block until
        the requested element(s) are found.

        it will block for a maximum of <timeout> seconds, after which
        an TimeoutError will be raised

        :param selector: css selector
        :type selector:
        :param text: text
        :type text:
        :param timeout:
        :type timeout:
        :return:
        :rtype: Element
        :raises: asyncio.TimeoutError
        """
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        if selector:
            item = await self.query_selector(selector)
            while not item and loop.time() - start_time < timeout:
                item = await self.query_selector(selector)
                await self.sleep(0.5)

            if item:
                return item
        if text:
            item = await self.find_element_by_text(text)
            while not item and loop.time() - start_time < timeout:
                item = await self.find_element_by_text(text)
                await self.sleep(0.5)

            if item:
                return item

        raise asyncio.TimeoutError("time ran out while waiting")

    async def wait_for_ready_state(
        self,
        until: Literal["loading", "interactive", "complete"] = "interactive",
        timeout: int = 10,
    ) -> bool:
        """
        Waits for the page to reach a certain ready state.
        :param until: The ready state to wait for. Can be one of "loading", "interactive", or "complete".
        :type until: str
        :param timeout: The maximum number of seconds to wait.
        :type timeout: int
        :raises asyncio.TimeoutError: If the timeout is reached before the ready state is reached.
        :return: True if the ready state is reached.
        :rtype: bool
        """
        loop = asyncio.get_event_loop()
        start_time = loop.time()

        while True:
            ready_state = await self.evaluate("document.readyState")
            if ready_state == until:
                return True

            if loop.time() - start_time > timeout:
                raise asyncio.TimeoutError(
                    "time ran out while waiting for load page until %s" % until
                )

            await asyncio.sleep(0.1)

    def expect_request(
        self, url_pattern: Union[str, re.Pattern[str]]
    ) -> RequestExpectation:
        """
        Creates a request expectation for a specific URL pattern.
        :param url_pattern: The URL pattern to match requests.
        :type url_pattern: Union[str, re.Pattern[str]]
        :return: A RequestExpectation instance.
        :rtype: RequestExpectation
        """
        return RequestExpectation(self, url_pattern)

    def expect_response(
        self, url_pattern: Union[str, re.Pattern[str]]
    ) -> ResponseExpectation:
        """
        Creates a response expectation for a specific URL pattern.
        :param url_pattern: The URL pattern to match responses.
        :type url_pattern: Union[str, re.Pattern[str]]
        :return: A ResponseExpectation instance.
        :rtype: ResponseExpectation
        """
        return ResponseExpectation(self, url_pattern)

    def expect_download(self) -> DownloadExpectation:
        """
        Creates a download expectation for next download.
        :return: A DownloadExpectation instance.
        :rtype: DownloadExpectation
        """
        return DownloadExpectation(self)

    def intercept(
        self,
        url_pattern: str,
        request_stage: RequestStage,
        resource_type: ResourceType,
    ) -> BaseFetchInterception:
        """
        Sets up interception for network requests matching a URL pattern, request stage, and resource type.

        :param url_pattern: URL string or regex pattern to match requests.
        :type url_pattern: Union[str, re.Pattern[str]]
        :param request_stage: Stage of the request to intercept (e.g., request, response).
        :type request_stage: RequestStage
        :param resource_type: Type of resource (e.g., Document, Script, Image).
        :type resource_type: ResourceType
        :return: A BaseFetchInterception instance for further configuration or awaiting intercepted requests.
        :rtype: BaseFetchInterception

        Use this to block, modify, or inspect network traffic for specific resources during browser automation.
        """
        return BaseFetchInterception(self, url_pattern, request_stage, resource_type)

    async def download_file(
        self, url: str, filename: Optional[PathLike] = None
    ) -> None:
        """
        downloads file by given url.

        :param url: url of the file
        :param filename: the name for the file. if not specified the name is composed from the url file name
        """
        if not self._download_behavior:
            directory_path = pathlib.Path.cwd() / "downloads"
            directory_path.mkdir(exist_ok=True)
            await self.set_download_path(directory_path)

            warnings.warn(
                f"no download path set, so creating and using a default of"
                f"{directory_path}"
            )
        if not filename:
            filename = url.rsplit("/")[-1]
            filename = filename.split("?")[0]

        code = """
         (elem) => {
            async function _downloadFile(
              imageSrc,
              nameOfDownload,
            ) {
              const response = await fetch(imageSrc);
              const blobImage = await response.blob();
              const href = URL.createObjectURL(blobImage);

              const anchorElement = document.createElement('a');
              anchorElement.href = href;
              anchorElement.download = nameOfDownload;

              document.body.appendChild(anchorElement);
              anchorElement.click();

              setTimeout(() => {
                document.body.removeChild(anchorElement);
                window.URL.revokeObjectURL(href);
                }, 500);
            }
            _downloadFile('%s', '%s')
            }
            """ % (
            url,
            filename,
        )

        body = (await self.query_selector_all("body"))[0]
        await body.update()
        await self.send(
            cdp.runtime.call_function_on(
                code,
                object_id=body.object_id,
                arguments=[cdp.runtime.CallArgument(object_id=body.object_id)],
            )
        )

    async def save_snapshot(self, filename: str = "snapshot.mhtml") -> None:
        """
        Saves a snapshot of the page.
        :param filename: The save path; defaults to "snapshot.mhtml"
        """
        await self.sleep()  # update the target's url
        path = pathlib.Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = await self.send(cdp.page.capture_snapshot())
        if not data:
            raise ProtocolException(
                "Could not take snapshot. Most possible cause is the page has not finished loading yet."
            )

        with open(filename, "w") as file:
            file.write(data)

    async def screenshot_b64(
        self,
        format: str = "jpeg",
        full_page: bool = False,
    ) -> str:
        """
        Takes a screenshot of the page and return the result as a base64 encoded string.
        This is not the same as :py:obj:`Element.screenshot_b64`, which takes a screenshot of a single element only

        :param format: jpeg or png (defaults to jpeg)
        :type format: str
        :param full_page: when False (default) it captures the current viewport. when True, it captures the entire page
        :type full_page: bool
        :return: screenshot data as base64 encoded
        :rtype: str
        """
        if self.target is None:
            raise ValueError("target is none")

        await self.sleep()  # update the target's url

        if format.lower() in ["jpg", "jpeg"]:
            format = "jpeg"
        elif format.lower() in ["png"]:
            format = "png"

        data = await self.send(
            cdp.page.capture_screenshot(
                format_=format, capture_beyond_viewport=full_page
            )
        )
        if not data:
            raise ProtocolException(
                "could not take screenshot. most possible cause is the page has not finished loading yet."
            )

        return data

    async def save_screenshot(
        self,
        filename: Optional[PathLike] = "auto",
        format: str = "jpeg",
        full_page: bool = False,
    ) -> str:
        """
        Saves a screenshot of the page.
        This is not the same as :py:obj:`Element.save_screenshot`, which saves a screenshot of a single element only

        :param filename: uses this as the save path
        :type filename: PathLike
        :param format: jpeg or png (defaults to jpeg)
        :type format: str
        :param full_page: when False (default) it captures the current viewport. when True, it captures the entire page
        :type full_page: bool
        :return: the path/filename of saved screenshot
        :rtype: str
        """
        if format.lower() in ["jpg", "jpeg"]:
            ext = ".jpg"

        elif format.lower() in ["png"]:
            ext = ".png"

        if not filename or filename == "auto":
            assert self.target is not None
            parsed = urllib.parse.urlparse(self.target.url)
            parts = parsed.path.split("/")
            last_part = parts[-1]
            last_part = last_part.rsplit("?", 1)[0]
            dt_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            candidate = f"{parsed.hostname}__{last_part}_{dt_str}"
            path = pathlib.Path(candidate + ext)  # noqa
        else:
            path = pathlib.Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = await self.screenshot_b64(format=format, full_page=full_page)

        data_bytes = base64.b64decode(data)
        if not path:
            raise RuntimeError("invalid filename or path: '%s'" % filename)
        path.write_bytes(data_bytes)
        return str(path)

    async def print_to_pdf(self, filename: PathLike, **kwargs: Any) -> pathlib.Path:
        """
        Prints the current page to a PDF file and saves it to the specified path.

        :param filename: The path where the PDF will be saved.
        :param kwargs: Additional options for printing to be passed to :py:obj:`cdp.page.print_to_pdf`.
        :return: The path to the saved PDF file.
        :rtype: pathlib.Path
        """
        filename = pathlib.Path(filename)
        if filename.is_dir():
            raise ValueError(
                f"filename {filename} must be a file path, not a directory"
            )

        data, _ = await self.send(cdp.page.print_to_pdf(**kwargs))

        data_bytes = base64.b64decode(data)
        filename.write_bytes(data_bytes)
        return filename

    async def set_download_path(self, path: PathLike) -> None:
        """
        sets the download path and allows downloads
        this is required for any download function to work (well not entirely, since when unset we set a default folder)

        :param path:
        :type path:
        :return:
        :rtype:
        """
        path = pathlib.Path(path)
        await self.send(
            cdp.browser.set_download_behavior(
                behavior="allow", download_path=str(path.resolve())
            )
        )
        self._download_behavior = ["allow", str(path.resolve())]

    async def get_all_linked_sources(self) -> List[Element]:
        """
        get all elements of tag: link, a, img, scripts meta, video, audio

        :return:
        """
        all_assets = await self.query_selector_all(selector="a,link,img,script,meta")
        return [element.create(asset.node, self) for asset in all_assets]

    async def get_all_urls(self, absolute: bool = True) -> List[str]:
        """
        convenience function, which returns all links (a,link,img,script,meta)

        :param absolute: try to build all the links in absolute form instead of "as is", often relative
        :return: list of urls
        """

        import urllib.parse

        res: list[str] = []
        all_assets = await self.query_selector_all(selector="a,link,img,script,meta")
        for asset in all_assets:
            if not absolute:
                res_to_add = asset.src or asset.href
                if res_to_add:
                    res.append(res_to_add)
            else:
                for k, v in asset.attrs.items():
                    if k in ("src", "href"):
                        if "#" in v:
                            continue
                        if not any([_ in v for _ in ("http", "//", "/")]):
                            continue
                        abs_url = urllib.parse.urljoin(
                            "/".join(self.url.rsplit("/")[:3] if self.url else []), v
                        )
                        if not abs_url.startswith(("http", "//", "ws")):
                            continue
                        res.append(abs_url)
        return res

    async def verify_cf(
        self,
        click_delay: float = 5,
        timeout: float = 15,
        challenge_selector: Optional[str] = None,
        flash_corners: bool = False,
    ) -> None:
        """
        Finds and solves the Cloudflare checkbox challenge.

        The total time for finding and clicking is governed by `timeout`.

        Args:
            click_delay: The delay in seconds between clicks.
            timeout: The total time in seconds to wait for the challenge and solve it.
            challenge_selector: An optional CSS selector for the challenge input element.
            flash_corners: If True, flash the corners of the challenge element.

        Raises:
            TimeoutError: If the checkbox is not found or solved within the timeout.
        """
        from .cloudflare import verify_cf

        await verify_cf(self, click_delay, timeout, challenge_selector, flash_corners)

    async def mouse_move(
        self, x: float, y: float, steps: int = 10, flash: bool = False
    ) -> None:
        steps = 1 if (not steps or steps < 1) else steps
        # probably the worst waay of calculating this. but couldn't think of a better solution today.
        if steps > 1:
            step_size_x = x // steps
            step_size_y = y // steps
            pathway = [(step_size_x * i, step_size_y * i) for i in range(steps + 1)]
            for point in pathway:
                if flash:
                    await self.flash_point(point[0], point[1])
                await self.send(
                    cdp.input_.dispatch_mouse_event(
                        "mouseMoved", x=point[0], y=point[1]
                    )
                )
        else:
            await self.send(cdp.input_.dispatch_mouse_event("mouseMoved", x=x, y=y))
        if flash:
            await self.flash_point(x, y)
        else:
            await self.sleep(0.05)
        await self.send(cdp.input_.dispatch_mouse_event("mouseReleased", x=x, y=y))
        if flash:
            await self.flash_point(x, y)

    async def mouse_click(
        self,
        x: float,
        y: float,
        button: str = "left",
        buttons: typing.Optional[int] = 1,
        modifiers: typing.Optional[int] = 0,
        _until_event: typing.Optional[type] = None,
        flash: typing.Optional[bool] = False,
    ) -> None:
        """native click on position x,y
        :param y:
        :type y:
        :param x:
        :type x:
        :param button: str (default = "left")
        :param buttons: which button (default 1 = left)
        :param modifiers: *(Optional)* Bit field representing pressed modifier keys.
                Alt=1, Ctrl=2, Meta/Command=4, Shift=8 (default: 0).
        :param _until_event: internal. event to wait for before returning
        :return:
        """

        await self.send(
            cdp.input_.dispatch_mouse_event(
                "mousePressed",
                x=x,
                y=y,
                modifiers=modifiers,
                button=cdp.input_.MouseButton(button),
                buttons=buttons,
                click_count=1,
            )
        )

        await self.send(
            cdp.input_.dispatch_mouse_event(
                "mouseReleased",
                x=x,
                y=y,
                modifiers=modifiers,
                button=cdp.input_.MouseButton(button),
                buttons=buttons,
                click_count=1,
            )
        )
        if flash:
            await self.flash_point(x, y)

    async def flash_point(
        self, x: float, y: float, duration: float = 0.5, size: int = 10
    ) -> None:
        style = (
            "position:fixed;z-index:99999999;padding:0;margin:0;"
            "left:{:.1f}px; top: {:.1f}px;"
            "opacity:1;"
            "width:{:d}px;height:{:d}px;border-radius:50%;background:red;"
            "animation:show-pointer-ani {:.2f}s ease 1;"
        ).format(x - 8, y - 8, size, size, duration)
        script = (
            """
                var css = document.styleSheets[0];
                for( let css of [...document.styleSheets]) {{
                    try {{
                        css.insertRule(`
                        @keyframes show-pointer-ani {{
                              0% {{ opacity: 1; transform: scale(1, 1);}}
                              50% {{ transform: scale(3, 3);}}
                              100% {{ transform: scale(1, 1); opacity: 0;}}
                        }}`,css.cssRules.length);
                        break;
                    }} catch (e) {{
                        console.log(e)
                    }}
                }};
                var _d = document.createElement('div');
                _d.style = `{0:s}`;
                _d.id = `{1:s}`;
                document.body.insertAdjacentElement('afterBegin', _d);

                setTimeout( () => document.getElementById('{1:s}').remove(), {2:d});

            """.format(style, secrets.token_hex(8), int(duration * 1000))
            .replace("  ", "")
            .replace("\n", "")
        )
        await self.send(
            cdp.runtime.evaluate(
                script,
                await_promise=True,
                user_gesture=True,
            )
        )

    async def get_local_storage(self) -> dict[str, str]:
        """
        get local storage items as dict of strings (careful!, proper deserialization needs to be done if needed)

        :return:
        :rtype:
        """
        if self.target is None or not self.target.url:
            await self.wait()

        # there must be a better way...
        origin = "/".join(self.url.split("/", 3)[:-1] if self.url else [])

        items = await self.send(
            cdp.dom_storage.get_dom_storage_items(
                cdp.dom_storage.StorageId(is_local_storage=True, security_origin=origin)
            )
        )
        retval: dict[str, str] = {}
        for item in items:
            retval[item[0]] = item[1]
        return retval

    async def set_local_storage(self, items: dict[str, str]) -> None:
        """
        set local storage.
        dict items must be strings. simple types will be converted to strings automatically.

        :param items: dict containing {key:str, value:str}
        :type items: dict[str,str]
        :return:
        :rtype:
        """
        if self.target is None or not self.target.url:
            await self.wait()
        # there must be a better way...
        origin = "/".join(self.url.split("/", 3)[:-1] if self.url else [])

        await asyncio.gather(
            *[
                self.send(
                    cdp.dom_storage.set_dom_storage_item(
                        storage_id=cdp.dom_storage.StorageId(
                            is_local_storage=True, security_origin=origin
                        ),
                        key=str(key),
                        value=str(val),
                    )
                )
                for key, val in items.items()
            ]
        )

    async def set_user_agent(
        self,
        user_agent: str | None = None,
        accept_language: str | None = None,
        platform: str | None = None,
    ) -> None:
        """
        Set the user agent, accept language, and platform.

        These correspond to:
            - navigator.userAgent
            - navigator.language
            - navigator.platform

        Note: In most cases, you should instead pass the user_agent option to zendriver.start().
        This ensures that the user agent is set before the browser starts and correctly applies to
        all pages and requests.

        :param user_agent: user agent string
        :type user_agent: str
        :param accept_language: accept language string
        :type accept_language: str
        :param platform: platform string
        :type platform: str
        :return:
        :rtype:
        """
        if not user_agent:
            user_agent = await self.evaluate("navigator.userAgent")  # type: ignore
            if not user_agent:
                raise ValueError(
                    "Could not read existing user agent from navigator object"
                )

        await self.send(
            cdp.network.set_user_agent_override(
                user_agent=user_agent,
                accept_language=accept_language,
                platform=platform,
            )
        )

    async def __call__(
        self,
        text: str | None = None,
        selector: str | None = None,
        timeout: int | float = 10,
    ) -> Element:
        """
        alias to query_selector_all or find_elements_by_text, depending
        on whether text= is set or selector= is set

        :param selector: css selector string
        :type selector: str
        :return:
        :rtype:
        """
        return await self.wait_for(text, selector, timeout)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Tab):
            return False

        return other.target == self.target

    def __repr__(self) -> str:
        extra = ""
        if self.target is not None and self.target.url:
            extra = f"[url: {self.target.url}]"
        s = f"<{type(self).__name__} [{self.target_id}] [{self.type_}] {extra}>"
        return s
