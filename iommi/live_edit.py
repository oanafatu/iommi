import json
from pathlib import Path

from django.conf import settings
from django.http import (
    HttpResponse,
    HttpResponseRedirect,
)
from django.views.decorators.csrf import csrf_exempt
from django.utils import autoreload
from tri_struct import Struct

from iommi import *
from iommi import (
    render_if_needed,
    Page,
    Header,
    html,
    Menu,
    MenuItem,
    Form,
    Field,
    Action,
    Table,
    Column,
)
from iommi._web_compat import mark_safe
import parso

from iommi.base import items

orig_reload = getattr(autoreload, 'trigger_reload', None)


class Middleware:
    """
    The live edit middleware enables editing of the source code of views with as-you-type results, inside the web browser.

    Note: This middleware needs to go *first* in the middleware list.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, callback, callback_args, callback_kwargs):
        if should_edit(request):
            return live_edit_dispatch(request)(request=request, view=callback)


def live_edit_dispatch(request):
    return {
        '': live_edit_view,
        'style_showcase': style_showcase,
        'style_editor__edit': style_editor__edit,
        'style_editor__new': style_editor__new,
        'style_editor': style_editor__select,
    }[request.GET['_iommi_live_edit']]


def should_edit(request):
    return settings.DEBUG and '_iommi_live_edit' in request.GET


def get_wrapped_view(view):
    if hasattr(view, '__iommi_target__'):
        view = view.__iommi_target__

    while hasattr(view, '__wrapped__'):
        view = view.__wrapped__

    return view


def include_decorators(node):
    while node.parent.type == 'decorated':
        node = node.parent
    return node


def find_node(*, name, node, node_type):
    """
    node_type should be either funcdef or classdef
    """
    if node.type == node_type:
        if getattr(node, 'name', Struct(value=None)).value == name:
            return node
    for child_node in getattr(node, 'children', []):
        r = find_node(node=child_node, name=name, node_type=node_type)
        if r is not None:
            return include_decorators(r)
    return None


def find_view(view, ast_of_entire_file):
    if isinstance(view, Part):
        return find_node(name=type(view).__name__, node=ast_of_entire_file, node_type='classdef')
    else:
        return find_node(name=view.__name__, node=ast_of_entire_file, node_type='funcdef')


def live_edit_post_handler(request, code, view, filename, create_response, write_new_code_to_disk, **params):
    try:
        new_view = dangerous_execute_code(code, request, view)

        response = render_if_needed(request, create_response(new_view, request=request, **params))
        final_result = HttpResponse(json.dumps(dict(page=response.content.decode())))

        if orig_reload is not None:
            # A little monkey patch dance to avoid one reload of the runserver when it's just us writing the code to disk
            # This only works in django 2.2+
            def restore_auto_reload(filename):
                from django.utils import autoreload
                print('Skipped reload')
                autoreload.trigger_reload = orig_reload

            autoreload.trigger_reload = restore_auto_reload

        write_new_code_to_disk(code=code, filename=filename, view=view, **params)

        return final_result
    except Exception as e:
        import traceback
        traceback.print_exc()
        error = str(e)
        if not error:
            error = str(e.__class__)
        return HttpResponse(json.dumps(dict(error=error)))


def write_new_code_to_disk_for_view(ast_of_entire_file, ast_of_old_code, code, filename, view, **_):
    if isinstance(view, Part):
        ast_of_new_code = find_node(name=view.__class__.__name__, node=parso.parse(code), node_type='classdef')
    else:
        ast_of_new_code = find_node(name=view.__name__, node=parso.parse(code), node_type='funcdef')
    ast_of_old_code.children[:] = ast_of_new_code.children
    new_code = ast_of_entire_file.get_code()
    with open(filename, 'w') as f:
        f.write(new_code)


def create_response_for_view(new_view, request, **_):
    if isinstance(new_view, type) and issubclass(new_view, Part):
        return new_view().bind(request=request).render_to_response()
    else:
        return new_view(request)


@csrf_exempt
def live_edit_view(request, view):
    view = get_wrapped_view(view)
    # Read the old code
    try:
        # view is a function based view
        filename = view.__globals__['__file__']
    except AttributeError:
        # view is an iommi class
        from iommi.debug import filename_and_line_num_from_part
        filename, _ = filename_and_line_num_from_part(view)

    def build_params(entire_file, **_):
        ast_of_entire_file = parso.parse(entire_file)

        ast_of_old_code = find_view(view, ast_of_entire_file)
        assert ast_of_old_code is not None
        return dict(
            ast_of_entire_file=ast_of_entire_file,
            ast_of_old_code=ast_of_old_code,
        )

    flow_direction = request.GET.get('_iommi_live_edit') or 'column'
    assert flow_direction in ('column', 'row')

    return live_edit_view_impl(
        request,
        view=view,
        filename=filename,
        build_params=build_params,
        create_response=create_response_for_view,
        get_code=lambda ast_of_old_code, **_: ast_of_old_code.get_code(),
        write_new_code_to_disk=write_new_code_to_disk_for_view,
        flow_direction=flow_direction,
    )


def live_edit_view_impl(request, view, filename, build_params, get_code, create_response, write_new_code_to_disk, flow_direction):
    with open(filename) as f:
        entire_file = f.read()

    is_unix_line_endings = '\r\n' not in entire_file

    params = {
        'entire_file': entire_file,
        'is_unix_line_endings': is_unix_line_endings,
        'view': view,
        'filename': filename,
        'request': request,
        'create_response': create_response,
        'write_new_code_to_disk': write_new_code_to_disk,
    }
    params = {
        **params,
        **build_params(**params),
    }

    if request.method == 'POST':
        code = request.POST['data'].replace('\t', '    ')
        if is_unix_line_endings:
            code = code.replace('\r\n', '\n')
        params['code'] = code
        return live_edit_post_handler(**params)

    code = get_code(**params)

    # This class exists just to provide a way to style the page
    class LiveEditPage(Page):
        pass

    return LiveEditPage(
        assets__code_editor=Asset.js(
            attrs=dict(
                src='https://cdnjs.cloudflare.com/ajax/libs/ace/1.4.12/ace.js',
                integrity='sha512-GZ1RIgZaSc8rnco/8CXfRdCpDxRCphenIiZ2ztLy3XQfCbQUSCuk8IudvNHxkRA3oUg6q0qejgN/qqyG1duv5Q==',
                crossorigin='anonymous',
            ),
            after=-1,
        ),
        assets__custom=Asset(
            tag='style',
            text='''
            .container {
                padding: 0 !important;           
                margin: 0 !important;
                max-width: 100%;
            }

            html,
            body {
                height: 100%;
                margin: 0;
            }

            .container {
                display: flex;
                flex-flow: <<flow_direction>>;
                height: 100%;
            }

            .container iframe {
                flex: 1 1 auto;
            }
            .container #editor_and_error {
                flex: 2 1 auto;
                display: flex;
                flex-flow: column;
            }
            #editor {
                flex: 2 1 auto;
            }
            '''.replace('<<flow_direction>>', flow_direction)
        ),

        iommi_style='bootstrap',

        parts__result=html.iframe(attrs__id='result'),
        parts__editor_and_error=html.div(
            attrs__id='editor_and_error',
            children=dict(
                editor=html.div(
                    code,
                    attrs__id='editor',
                ),
                error=html.div(attrs__id='error'),
            ),
        ),

        parts__script=html.script(mark_safe('''
        function iommi_debounce(func, wait) {
            let timeout;

            return (...args) => {
                const fn = () => func.apply(this, args);

                clearTimeout(timeout);
                timeout = setTimeout(() => fn(), wait);
            };
        }

        var editor = ace.edit("editor");
        editor.setTheme("ace/theme/cobalt");
        editor.session.setMode("ace/mode/python");
        editor.setShowPrintMargin(false);

        async function update() {
            let form_data = new FormData();
            form_data.append('data', editor.getValue());

            let response = await fetch('', {
                method: 'POST',
                body: form_data
            });
            let foo = await response.json();
            if (foo.page) {
                // TODO: get scroll position and restore it
                document.getElementById('result').srcdoc = foo.page;
            }
            document.getElementById('error').innerText = foo.error || '';
        }


        function foo() {
            iommi_debounce(update, 200)();
        }

        editor.session.on('change', foo);
        editor.setFontSize(14);
        editor.session.setUseWrapMode(true);
        
        foo();
        ''')),
    )


def dangerous_execute_code(code, request, view):
    local_variables = {}
    if isinstance(view, Part):
        from iommi.debug import frame_from_part
        frame = frame_from_part(view)
        exec(code, frame.f_globals, local_variables)
    elif isinstance(view, Style):
        frame = view._instantiated_at_frame.f_back
        exec(code, frame.f_globals, local_variables)
    else:
        exec(code, view.__globals__, local_variables)
    request.method = 'GET'
    return list(local_variables.values())[-1]


def style_showcase(request, style=None, **_):
    from django.contrib.auth.models import User

    if style is None:
        from iommi.style import DEFAULT_STYLE
        style = getattr(settings, 'IOMMI_DEFAULT_STYLE', DEFAULT_STYLE)

    class DummyRow:
        def __init__(self, idx):
            self.idx = idx

        def __getattr__(self, attr):
            _, _, shortcut = attr.partition('column_of_type_')
            s = f'{shortcut} #{self.idx}'
            if shortcut == 'link' or attr == 'link':
                class Link:
                    def get_absolute_url(self):
                        return '#'

                    def __str__(self):
                        return 'title'

                return Link()
            if shortcut == 'number':
                return f'{self.idx}'
            return s

        @staticmethod
        def get_absolute_url():
            return '#'

    return Page(
        iommi_style=style,
        parts=dict(
            title=Header('Style showcase'),
            menu__children=dict(
                menu_title=html.h2('Menu'),
                menu=Menu(
                    sub_menu=dict(
                        active=MenuItem(url=request.get_full_path()),  # full path do make this item active
                        inactive=MenuItem(),
                    )
                ),
            ),
            form=Form(
                title='Form',
                fields=dict(
                    text=Field.text(initial='initial'),
                    boolean=Field.boolean(),
                    boolean_selected=Field.boolean(initial=True),
                    radio=Field.radio(choices=['a', 'b', 'c'], initial='b'),
                ),
                actions__submit__post_handler=lambda **_: None,
                actions__secondary=Action.button(),
                actions__delete=Action.delete(display_name='Delete'),
                actions__icon=Action.icon('trash', display_name='Icon', attrs__href='#'),
            ),
            table=Table(
                title='Table',
                assets__ajax_enhance__template=None,
                model=User,
                columns={
                    t.__name__: dict(call_target=t, display_name=t.__name__)
                    for t in [Column.select, Column.edit, Column.delete, Column.boolean, Column.text, Column.number, Column.link, Column.icon]
                },
                columns__text__filter__include=True,
                columns__number__filter__include=True,
                rows=[DummyRow(i) for i in range(10)],
                page_size=2,
            ),
        ),
    )


def style_editor__select(**_):
    from iommi.style import _styles
    return Form(
        title='Select style to edit',
        fields__name=Field.choice(
            choices=[
                k
                for k, v in items(_styles)
                if not v.internal
            ]
        ),
        actions__edit=Action.primary(
            display_name='Edit',
            post_handler=lambda form, **_: HttpResponseRedirect(f'?_iommi_live_edit=style_editor__edit&name={form.fields.name.value}') if form.is_valid() else None,
        ),
        actions__new_style__attrs__href='?_iommi_live_edit=style_editor__new',
    )


@csrf_exempt
def style_editor__edit(request, **_):
    from iommi.style import get_style, register_style
    name = request.GET.get('name', request.POST.get('name'))
    assert name is not None
    style = get_style(name)
    filename = style._instantiated_at_frame.f_back.f_code.co_filename

    def create_response(view, **_):
        view._instantiated_at_frame = style._instantiated_at_frame  # otherwise we end up at <string> which is not what we want
        register_style(name, view, allow_overwrite=True)
        return style_showcase(request, style=name)

    def write_new_code_to_disk(code, **_):
        with open(filename, 'w') as f:
            f.write(code)

    return live_edit_view_impl(
        request,
        view=style,  # TODO: rename view.. "subject"?
        filename=filename,
        build_params=lambda **_: {},
        get_code=lambda entire_file, **_: entire_file,
        create_response=create_response,
        write_new_code_to_disk=write_new_code_to_disk,
        flow_direction='row',
    )


def style_editor__new(**_):
    def new_style(form, **_):
        if not form.is_valid():
            return

        base, _, new = form.fields.module.value.rpartition('.')
        module = __import__(base, fromlist=['_silly_importlib'])
        target_filename = Path(module.__file__).parent / (new + '.py')
        if target_filename.exists():
            form.add_error(f'File {target_filename} already exists')

        with open(target_filename, 'w') as f:
            f.write(f'''
from iommi.style import Style
from iommi.style_base import base
from iommi.asset import Asset

{new} = Style(
    base,
)
''')
            return Page(
                parts=dict(
                    title=Header('Style created... now what?'),
                    message=html.p(f'''
                    The style file was written to {target_filename}. 
                    Now you need to register this style in order to edit it. This is
                    typically done by adding `register_style('{new}')` into `on_ready`
                    of your `AppConfig`.
                    '''),
                    message2=html.p("When you've done that, you can proceed to "),
                    edit=html.a('edit it', attrs__href=f'?_iommi_live_edit=style_editor__edit&name={new}'),
                ),
            )

    return Form(
        title='Create new style',
        fields=dict(
            module=Field(),  # TODO: can we guess this in a smart way? maybe look at settings.__module__?
        ),
        actions__submit__post_handler=new_style,
    )
