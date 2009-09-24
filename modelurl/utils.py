"""
Classes to replace urls with macros.
"""

import re
import threading
from importpath import importpath

from urlmethods import urlsplit, urljoin, local_response
from urlmethods.threadmethod import threadmethod
from middleware import MACRO_RE

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.urlresolvers import RegexURLResolver
from django.template import Template

def macro(obj):
    """
    Return macro string for specified ``obj``.
    
    >>> from example.models import Page
    >>> from modelurl.middleware import MACRO_RE
    
    >>> page = Page.objects.get(pk=1)
    >>> macro(page)
    '{@ example.models.Page 1 @}'

    >>> bool(MACRO_RE.match(macro(page)))
    True
    """
    return '{@ %s.%s %s @}' % (obj.__class__.__module__, obj.__class__.__name__, obj.pk)

class ReplaceException(Exception):
    """
    Base exception class.
    """
    pass

class DoesNotFoundException(ReplaceException):
    """
    Specified url was not found.
    """
    pass

class AlreadyMacroException(ReplaceException):
    """
    Specified url already is macro.
    """
    pass

class NoPathException(ReplaceException):
    """
    Url has no path (only anchor or/and query).
    """
    pass

class ForeignException(ReplaceException):
    """
    Url has link to the foreign site.
    """
    pass


class BaseReplace(object):
    """
    Base class to replace urls.
    """
    
    def __init__(self, silent=False):
        """
        If ``silent`` is True then instance of this class must
        raise exceptions when urls can`t be replaces with macro.
        
        If ``silent`` is False then instance of this class must
        return source value.
        """
        self.silent = silent


def silentmethod(func):
    """
    Decorator to make function silent if specified
    """
    def wrapper(self, value, *args, **kwargs):
        try:
            return func(self, value, *args, **kwargs)
        except ReplaceException, exception:
            if self.silent:
                return value
            else:
                raise exception
    return wrapper


class ReplaceByDict(BaseReplace):
    """
    Replace urls with macros using dictionary.
    
    >>> from example.models import Page, Item
    
    >>> dictionary = {}
    >>> for page in Page.objects.all():
    ...     dictionary[page.get_absolute_url()] = macro(page)
    ...
    >>> for item in Item.objects.all():
    ...     dictionary[item.my_url()] = macro(item)
    ...
    >>> replace = ReplaceByDict(dictionary)
    
    >>> replace.url('/page_by_id/1')
    '{@ example.models.Page 1 @}'

    >>> replace.url('/page_by_id/2')
    Traceback (most recent call last):
        ...
    DoesNotFoundException

    >>> replace.text('<a href="/page_by_id/2">page</a> and /item_by_barcode/second')
    '<a href="/page_by_id/2">page</a> and {@ example.models.Item 2 @}'
    """

    def __init__(self, dict, *args, **kwargs):
        """
        Set ``dict`` dictionary for replace operations.
        Keys must be an url.
        Value must be macro (use ``modelurl.generate_macro`` function to get it).
        """
        super(ReplaceByDict, self).__init__(*args, **kwargs)
        self.source = dict
        self.dict = None
        self.list = None
        self.regexp = None
    
    def get_dict(self):
        if self.dict is None:
            self.dict = {}
            for key, value in self.source.iteritems():
                self.dict[key.lower()] = value
        return self.dict

    def get_list(self):
        """
        Return list with urls and macros.
        """
        if self.list is None:
            self.list = list(self.get_dict().iteritems())
        self.list.sort(reverse=True)
        return self.list

    def get_regexp(self):
        """
        Return regexp for all urls.
        """
        if self.regexp is None:
            self.regexp = re.compile('|'.join(['(%s)' % re.escape(key)
                for key, value in self.get_list()]), re.IGNORECASE)
        return self.regexp

    @silentmethod
    def url(self, value):
        """
        Return macro for specified ``value``.
        """
        try:
            return self.get_dict()[value.lower()]
        except KeyError:
            raise DoesNotFoundException
    
    def text(self, value):
        """
        Replace urls in ``text`` with macros.
        """
        def replace(match):
            for index, value in enumerate(match.groups()):
                if value is not None:
                    return self.get_list()[index][1]
            return ''
        return self.get_regexp().sub(replace, value)


# Create local variable for all threads
local = threading.local()
def render(func):
    def wrapper(self, context):
        if hasattr(local, 'modelurl_object'):
            # We don`t want to render content if required object was found
            # for this thread.  
            return ''
        if hasattr(local, 'modelurl_object_name'):
            # We want to get object for modelurl if object_name was specified
            # for this thread.
            try:
                local.modelurl_object = context[local.modelurl_object_name]
            except KeyError:
                # Just skip it, may be we get it later.
                pass
        return func(self, context)
    return wrapper

# Create lock to update Template only once
lock = threading.Lock()
lock.acquire()
if not hasattr(Template, 'render_by_model_url'):
    # Register another render function to provide ReplaceByView
    Template.render = render(Template.render)
    setattr(Template, 'render_by_model_url', True)
lock.release()

@threadmethod()
def object_from_view(path, query, object_name):
    local.modelurl_object_name = object_name
    response = local_response(path, query)
    if response.status_code != 200:
        raise DoesNotFoundException
    try:
        return local.modelurl_object
    except AttributeError:
        raise DoesNotFoundException

class ReplaceByView(BaseReplace):
    """
    Replace urls with macros using calling of view for specified url 
    and retrieve object from context.
    """
    
    SERVER_SCHEMES = ['http', ]
    
    def __init__(self, views=None, sites=None, send_query=False, *args, **kwargs):
        super(ReplaceByView, self).__init__(*args, **kwargs)
        if views is None:
            views = settings.MODELURL_VIEWS
        self.views = {}
        for view, name in views.iteritems():
            self.views[importpath(view)] = name
        if sites is None:
            sites = [site.domain for site in Site.objects.all()]
        self.sites = sites
        self.send_query = send_query

    @silentmethod
    def url(self, value):
        if MACRO_RE.match(value):
            raise AlreadyMacroException
        scheme, authority, path, query, fragment = urlsplit(value)
        if not scheme and not authority and not path:
            raise NoPathException
        if (scheme and scheme.lower() not in self.SERVER_SCHEMES) or\
            (authority and authority.lower() not in self.sites):
            raise ForeignException
        resolver = RegexURLResolver(r'^/', settings.ROOT_URLCONF)
        callback, callback_args, callback_kwargs = resolver.resolve(value)
        try:
            object_name = self.views[callback]
        except KeyError:
            raise DoesNotFoundException
        if self.send_query:
            send_query = query
        else:
            send_query = ''
        obj = object_from_view(path, send_query, object_name)
        path = generate_macro(obj.__class__, obj.id)
        value = urljoin(None, None, path, query, fragment)
        return value
