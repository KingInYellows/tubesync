import json
import os
from unittest.mock import patch

from .. import config
from .base import BridgeTestCase


class TokenConfigTestCase(BridgeTestCase):

    def test_no_token_file_configured_is_disabled(self):
        self.assertIsNone(config.get_token())
        self.assertFalse(config.bridge_enabled())

    def test_token_file_missing_on_disk_is_disabled(self):
        os.environ['MEDIANEST_BRIDGE_TOKEN_FILE'] = '/nonexistent/path/token.txt'
        self.assertIsNone(config.get_token())
        self.assertFalse(config.bridge_enabled())

    def test_empty_token_file_is_disabled(self):
        path = self.enable_bridge(token='   ')
        self.assertIsNone(config.get_token())
        self.assertFalse(config.bridge_enabled())
        self.assertTrue(os.path.exists(path))

    def test_valid_token_file_enables_bridge(self):
        self.enable_bridge(token='real-token')
        self.assertEqual(config.get_token(), 'real-token')
        self.assertTrue(config.bridge_enabled())

    def test_token_is_stripped(self):
        self.enable_bridge(token='  real-token  \n')
        self.assertEqual(config.get_token(), 'real-token')


class ReadOnlyConfigTestCase(BridgeTestCase):

    def test_defaults_to_read_only(self):
        self.assertTrue(config.is_read_only())

    def test_explicit_false_disables_read_only(self):
        os.environ['MEDIANEST_BRIDGE_READ_ONLY'] = 'false'
        self.assertFalse(config.is_read_only())

    def test_explicit_true(self):
        os.environ['MEDIANEST_BRIDGE_READ_ONLY'] = 'true'
        self.assertTrue(config.is_read_only())

    def test_garbage_value_fails_closed_to_read_only(self):
        os.environ['MEDIANEST_BRIDGE_READ_ONLY'] = 'nonsense'
        self.assertTrue(config.is_read_only())


class MaxBodyBytesConfigTestCase(BridgeTestCase):

    def test_default_is_65536(self):
        self.assertEqual(config.max_body_bytes(), 65536)

    def test_override(self):
        os.environ['MEDIANEST_BRIDGE_MAX_BODY_BYTES'] = '1024'
        self.assertEqual(config.max_body_bytes(), 1024)


class AllowedCidrsConfigTestCase(BridgeTestCase):

    def test_unset_returns_none(self):
        self.assertIsNone(config.allowed_cidrs())

    def test_single_cidr(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = '192.168.1.68/32'
        nets = config.allowed_cidrs()
        self.assertEqual(len(nets), 1)
        self.assertEqual(str(nets[0]), '192.168.1.68/32')

    def test_multiple_comma_separated(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = '192.168.1.68/32, 10.0.0.0/24'
        nets = config.allowed_cidrs()
        self.assertEqual(len(nets), 2)

    def test_malformed_entry_fails_closed_to_empty_list(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = 'not-a-cidr'
        self.assertEqual(config.allowed_cidrs(), [])


class UpstreamShaConfigTestCase(BridgeTestCase):

    def test_default_is_unknown(self):
        self.assertEqual(config.upstream_sha(), 'unknown')

    def test_override(self):
        os.environ['MEDIANEST_BRIDGE_UPSTREAM_SHA'] = 'a' * 40
        self.assertEqual(config.upstream_sha(), 'a' * 40)


class SourceDefaultsConfigTestCase(BridgeTestCase):
    '''
        config.source_defaults()'s parsing/merge/allowlist behavior. See
        SourceDefaultsValidationTestCase below for validate_source_defaults()
        (the layer that additionally runs SourceForm/run_edit_source_checks),
        and tests/test_write_sources.py::SourceDefaultsCreateTestCase for
        the end-to-end POST /sources wiring.
    '''

    def test_unset_returns_builtin_profile_for_both_types(self):
        defaults = config.source_defaults()
        self.assertEqual(defaults['channel'], config._BUILTIN_SOURCE_DEFAULTS_PROFILE)
        self.assertEqual(defaults['playlist'], config._BUILTIN_SOURCE_DEFAULTS_PROFILE)

    def test_empty_object_is_the_no_overrides_escape_hatch(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = '{}'
        self.assertEqual(config.source_defaults(), {'channel': {}, 'playlist': {}})

    def test_invalid_json_raises(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = '{not json'
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_non_object_top_level_raises(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = '[1, 2, 3]'
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_unknown_top_level_key_raises(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps({'bogus': {}})
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_non_object_per_type_value_raises(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps({'channel': 'nope'})
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_non_object_star_value_raises(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps({'*': 'nope'})
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_unknown_field_raises(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            # "*": {} covers playlist too, isolating this test to the
            # unknown-field check alone rather than also depending on
            # channel being checked before the (otherwise uncovered)
            # playlist type.
            {'*': {}, 'channel': {'not_a_real_field': True}},
        )
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_forbidden_contract_owned_field_raises(self):
        for field in ('source_type', 'key', 'name', 'directory'):
            with self.subTest(field=field):
                os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
                    {'*': {}, 'channel': {field: 'x'}},
                )
                with self.assertRaises(config.SourceDefaultsConfigError):
                    config.source_defaults()

    def test_uncovered_type_raises_a_configuration_error(self):
        '''
            A type named by neither its own key nor "*" must fail loudly
            -- silently falling back to "no overrides" for the uncovered
            type would let an operator who only configured `channel` get
            `playlist` sources created with no override and no signal
            that anything is different for that type.
        '''
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {'channel': {'write_nfo': True}},
        )
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_explicit_per_type_empty_object_is_an_allowed_opt_out(self):
        '''
            Unlike a merely-absent type key (an error, see above), an
            explicitly present empty object satisfies coverage and is a
            full opt-out for that type alone -- it deliberately ignores
            "*" too, the same way the top-level {} escape hatch ignores
            everything.
        '''
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {'*': {'write_nfo': True}, 'channel': {'copy_thumbnails': True}, 'playlist': {}},
        )
        defaults = config.source_defaults()
        self.assertEqual(defaults['channel'], {'write_nfo': True, 'copy_thumbnails': True})
        self.assertEqual(defaults['playlist'], {})

    def test_star_alone_covers_both_types_with_no_per_type_key_at_all(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps({'*': {'write_nfo': True}})
        defaults = config.source_defaults()
        self.assertEqual(defaults['channel'], {'write_nfo': True})
        self.assertEqual(defaults['playlist'], {'write_nfo': True})

    def test_star_block_merges_under_both_types(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {'*': {'write_nfo': True}, 'channel': {'copy_thumbnails': True}},
        )
        defaults = config.source_defaults()
        self.assertEqual(defaults['channel'], {'write_nfo': True, 'copy_thumbnails': True})
        self.assertEqual(defaults['playlist'], {'write_nfo': True})

    def test_per_type_field_wins_over_star_on_conflict(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {'*': {'write_nfo': True}, 'channel': {'write_nfo': False}},
        )
        defaults = config.source_defaults()
        self.assertFalse(defaults['channel']['write_nfo'])
        self.assertTrue(defaults['playlist']['write_nfo'])


class SourceDefaultsFieldRulesTestCase(BridgeTestCase):
    '''Forbidden fields, boolean typing and the "*" block check.'''

    def set_defaults(self, value):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(value)

    def test_target_schedule_is_forbidden(self):
        self.set_defaults({'*': {'target_schedule': None}})
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()

    def test_non_boolean_value_for_a_boolean_field_raises(self):
        self.set_defaults({'*': {'write_nfo': '0'}})
        with self.assertRaises(config.SourceDefaultsConfigError) as ctx:
            config.source_defaults()
        self.assertIn('write_nfo', str(ctx.exception))
        self.assertNotIn("'0'", str(ctx.exception))

    def test_star_block_is_checked_even_when_both_types_opt_out(self):
        self.set_defaults(
            {'*': {'typo_field': 1}, 'channel': {}, 'playlist': {}},
        )
        with self.assertRaises(config.SourceDefaultsConfigError):
            config.source_defaults()


class SourceDefaultsValidationTestCase(BridgeTestCase):
    '''
        config.validate_source_defaults() -- source_defaults() parsing
        plus a real (never-saved) SourceForm.is_valid()/
        run_edit_source_checks() pass over each type's overlay.
    '''

    def test_builtin_profile_is_valid(self):
        self.assertEqual(config.validate_source_defaults(), [])

    def test_empty_escape_hatch_is_valid(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = '{}'
        self.assertEqual(config.validate_source_defaults(), [])

    def test_invalid_json_surfaces_as_a_single_error(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = '{not json'
        errors = config.validate_source_defaults()
        self.assertEqual(len(errors), 1)
        self.assertIn('not valid JSON', errors[0])

    def test_uncovered_type_surfaces_as_an_error(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {'channel': {'write_nfo': True}},
        )
        errors = config.validate_source_defaults()
        self.assertEqual(len(errors), 1)
        self.assertIn('playlist', errors[0])

    def test_media_format_that_cannot_produce_a_filename_is_invalid(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            # "*": {} covers playlist so this test isolates the
            # media_format check itself, not the coverage requirement.
            {'*': {}, 'channel': {'media_format': '{not_a_real_format_key}'}},
        )
        errors = config.validate_source_defaults()
        self.assertTrue(errors)
        self.assertTrue(any('channel' in message for message in errors))

    def test_valid_per_type_overlay_has_no_errors(self):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {'channel': {'write_nfo': True}, 'playlist': {'copy_thumbnails': True}},
        )
        self.assertEqual(config.validate_source_defaults(), [])

    def test_invalid_channel_overlay_does_not_block_a_valid_playlist_overlay_check(self):
        # Each type is validated independently -- a broken channel overlay
        # is reported, but validate_source_defaults() still checks (and
        # would report on) playlist too, rather than stopping at the
        # first failure. playlist gets a genuinely valid overlay of its
        # own here (not just "*": {}) so this test actually exercises
        # that independent check, not merely coverage.
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {
                'channel': {'media_format': '{not_a_real_format_key}'},
                'playlist': {'write_nfo': True},
            },
        )
        errors = config.validate_source_defaults()
        self.assertTrue(any(message.startswith('channel:') for message in errors))
        self.assertFalse(any(message.startswith('playlist:') for message in errors))

    def test_never_echoes_the_raw_env_value(self):
        secret_marker = 'super-secret-path-marker-should-not-leak'
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(
            {'*': {}, 'channel': {'media_format': secret_marker + '-{not_a_real_format_key}'}},
        )
        errors = config.validate_source_defaults()
        self.assertTrue(errors)
        self.assertFalse(any(secret_marker in message for message in errors))


class SourceDefaultsValidationSafetyTestCase(BridgeTestCase):
    '''Value-free errors, extra value checks and unexpected failures.'''

    def set_defaults(self, value):
        os.environ['MEDIANEST_BRIDGE_SOURCE_DEFAULTS'] = json.dumps(value)

    def test_invalid_choice_error_never_echoes_the_configured_value(self):
        marker = 'SECRET-MARKER-7f3a'
        self.set_defaults({'*': {'source_resolution': marker}})
        errors = config.validate_source_defaults()
        self.assertTrue(errors)
        self.assertTrue(any('source_resolution' in e for e in errors))
        self.assertFalse(any(marker in e for e in errors))

    def test_media_format_with_a_parent_segment_is_invalid(self):
        self.set_defaults(
            {'*': {'media_format': '../escape/{key}.{ext}'}},
        )
        errors = config.validate_source_defaults()
        self.assertTrue(any('media_format' in e and '..' in e for e in errors))

    def test_invalid_filter_text_regex_is_invalid(self):
        self.set_defaults({'*': {'filter_text': '(unclosed'}})
        errors = config.validate_source_defaults()
        self.assertTrue(any('filter_text' in e for e in errors))
        self.assertFalse(any('(unclosed' in e for e in errors))

    def test_list_shaped_field_accepts_a_string_or_a_list(self):
        for value in ('sponsor', ['sponsor']):
            with self.subTest(value=value):
                self.set_defaults({'*': {'sponsorblock_categories': value}})
                self.assertEqual(config.validate_source_defaults(), [])

    def test_unexpected_exception_is_reported_not_raised(self):
        self.set_defaults({'*': {'write_nfo': True}})
        with patch(
            'medianest_bridge.source_forms.build_synthetic_source_form',
            side_effect=RuntimeError('boom'),
        ):
            defaults, errors = config.load_validated_source_defaults()
        self.assertEqual(len(errors), 2)
        self.assertTrue(all('unexpected validation failure' in e for e in errors))
        self.assertIsNotNone(defaults)

    def test_load_returns_the_parsed_overlays(self):
        self.set_defaults({'*': {'write_nfo': True}})
        defaults, errors = config.load_validated_source_defaults()
        self.assertEqual(errors, [])
        self.assertEqual(defaults['channel'], {'write_nfo': True})
