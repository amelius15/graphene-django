from collections import OrderedDict

import six

from django.utils.functional import SimpleLazyObject
from graphene import Field, ObjectType, Boolean
from graphene.types.objecttype import ObjectTypeMeta
from graphene.types.options import Options
from graphene.types.utils import merge, yank_fields_from_attrs
from graphene.utils.is_base_type import is_base_type
from rest_framework import serializers
from graphene.relay.mutation import ClientIDMutationMeta, ClientIDMutation

from .converter import convert_django_field_with_choices
from .registry import Registry, get_global_registry
from .utils import (DJANGO_FILTER_INSTALLED, get_model_fields,
                    is_valid_django_model)


def construct_fields(options):
    _model_fields = get_model_fields(options.model)
    only_fields = options.only_fields
    exclude_fields = options.exclude_fields

    fields = OrderedDict()
    for field in _model_fields:
        name = field.name
        is_not_in_only = only_fields and name not in options.only_fields
        is_already_created = name in options.fields
        is_excluded = name in exclude_fields or is_already_created
        if is_not_in_only or is_excluded:
            # We skip this field if we specify only_fields and is not
            # in there. Or when we exclude this field in exclude_fields
            continue
        converted = convert_django_field_with_choices(field, options.registry)
        if not converted:
            continue
        fields[name] = converted

    return fields


class DjangoObjectTypeMeta(ObjectTypeMeta):

    @staticmethod
    def __new__(cls, name, bases, attrs):
        # Also ensure initialization is only performed for subclasses of
        # DjangoObjectType
        if not is_base_type(bases, DjangoObjectTypeMeta):
            return type.__new__(cls, name, bases, attrs)

        defaults = dict(
            name=name,
            description=attrs.pop('__doc__', None),
            model=None,
            local_fields=None,
            only_fields=(),
            exclude_fields=(),
            interfaces=(),
            registry=None
        )
        if DJANGO_FILTER_INSTALLED:
            # In case Django filter is available, then
            # we allow more attributes in Meta
            defaults.update(
                filter_fields=(),
                filter_order_by=(),
            )

        options = Options(
            attrs.pop('Meta', None),
            **defaults
        )
        if not options.registry:
            options.registry = get_global_registry()
        assert isinstance(options.registry, Registry), (
            'The attribute registry in {}.Meta needs to be an instance of '
            'Registry, received "{}".'
        ).format(name, options.registry)
        assert is_valid_django_model(options.model), (
            'You need to pass a valid Django Model in {}.Meta, received "{}".'
        ).format(name, options.model)

        cls = ObjectTypeMeta.__new__(cls, name, bases, dict(attrs, _meta=options))

        options.registry.register(cls)

        options.django_fields = yank_fields_from_attrs(
            construct_fields(options),
            _as=Field,
        )
        options.fields = merge(
            options.interface_fields,
            options.django_fields,
            options.base_fields,
            options.local_fields
        )

        return cls


class DjangoObjectType(six.with_metaclass(DjangoObjectTypeMeta, ObjectType)):

    def resolve_id(self, args, context, info):
        return self.pk

    @classmethod
    def is_type_of(cls, root, context, info):
        if isinstance(root, SimpleLazyObject):
            root._setup()
            root = root._wrapped
        if isinstance(root, cls):
            return True
        if not is_valid_django_model(type(root)):
            raise Exception((
                'Received incompatible instance "{}".'
            ).format(root))
        model = root._meta.model
        return model == cls._meta.model

    @classmethod
    def get_node(cls, id, context, info):
        try:
            return cls._meta.model.objects.get(pk=id)
        except cls._meta.model.DoesNotExist:
            return None

def make_model_serializer(target_model):
    class EditModelSerializer(serializers.ModelSerializer):
        class Meta:
            model = target_model

    return EditModelSerializer

class DjangoMutationMeta(ClientIDMutationMeta):
    @staticmethod
    def __new__(cls, name, bases, attrs):
        # Also ensure initialization is only performed for subclasses of
        # DjangoMutation
        if not is_base_type(bases, DjangoMutationMeta):
            return type.__new__(cls, name, bases, attrs)

        defaults = dict(
            name=name,
            description=attrs.pop('__doc__', None),
            model=None,
            local_fields=None,
            only_fields=(),
            exclude_fields=(),
            interfaces=(),
            registry=None,
            fields=()
        )

        meta = attrs.pop('MutationMeta')
        for default, value in defaults.items():
            if not hasattr(meta, default):
                setattr(meta, default, value)

        meta.serializer = make_model_serializer(meta.model)

        model_fields = construct_fields(meta)

        # input_attrs = model_fields
        input_class = type('Input', (object, ), model_fields)

        attributes = dict(attrs,
            ok=Boolean(),
            Input=input_class)
        if meta.result:
            attributes['result'] = Field(meta.result)

        parent = ClientIDMutationMeta.__new__(cls, name, bases, attributes)

        parent._mutation = meta

        return parent

@six.add_metaclass(DjangoMutationMeta)
class DjangoMutation(ClientIDMutation):
    @classmethod
    def mutate_and_get_payload(cls, input, context, info):
        instance = cls.get_instance(input, context, info)

        data = input
        for key, value in data.items():
            setattr(instance, key, value)

        instance.save()

        result = cls._mutation.model.objects.get(pk=instance.pk)

        return cls(ok=True, result=result)

