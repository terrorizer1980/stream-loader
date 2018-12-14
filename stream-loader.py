#! /usr/bin/env python

# -----------------------------------------------------------------------------
# stream-loader.py Loader for streaming input.
# -----------------------------------------------------------------------------

import argparse
import configparser
import confluent_kafka
from gevent import monkey
import gevent
from gevent.queue import Queue, Empty
from glob import glob
import json
import logging
import os
import signal
import sys
import time
import urllib2
from urlparse import urlparse

# Import Senzing libraries.

try:
    from G2ConfigTables import G2ConfigTables
    from G2Database import G2Database
    from G2Engine import G2Engine
    import G2Exception
    from G2Product import G2Product
    from G2Project import G2Project
except:
    pass

monkey.patch_all()

__all__ = []
__version__ = 1.0
__date__ = '2018-10-29'
__updated__ = '2018-12-14'

SENZING_PRODUCT_ID = "5001"  # See https://github.com/Senzing/knowledge-base/blob/master/lists/senzing-product-ids.md
log_format = '%(asctime)s %(message)s'

jsonlines_queue = Queue()

# Working with bytes.

KILOBYTES = 1024
MEGABYTES = 1024 * KILOBYTES
GIGABYTES = 1024 * MEGABYTES

MINIMUM_TOTAL_MEMORY_IN_GIGABYTES = 8
MINIMUM_AVAILABLE_MEMORY_IN_GIGABYTES = 6

# The "configuration_locator" describes where configuration variables are in:
# 1) Command line options, 2) Environment variables, 3) Configuration files, 4) Default values

configuration_locator = {
    "config_table_file": {
        "ini": {
            "section": "g2",
            "option": "G2ConfigFile"
        }
    },
    "data_source": {
        "default": None,
        "env": "SENZING_DATA_SOURCE",
        "cli": "data-source"
    },
    "debug": {
        "default": False,
        "env": "SENZING_DEBUG",
        "cli": "debug"
    },
    "entity_type": {
        "default": None,
        "env": "SENZING_ENTITY_TYPE",
        "cli": "entity-type"
    },
    "g2_database_url": {
        "ini": {
            "section": "g2",
            "option": "G2Connection"
        }
    },
    "g2_module_path": {
        "ini": {
            "section": "g2",
            "option": "iniPath"
        }
    },
    "input_url": {
        "default": None,
        "env": "SENZING_INPUT_URL",
        "cli": "input-url"
    },
    "kafka_bootstrap_server": {
        "default": "localhost:9092",
        "env": "SENZING_KAFKA_BOOTSTRAP_SERVER",
        "cli": "kafka-bootstrap-server",
    },
    "kafka_group": {
        "default": "senzing-kafka-group",
        "env": "SENZING_KAFKA_GROUP",
        "cli": "kafka-group"
    },
    "kafka_topic": {
        "default": "senzing-kafka-topic",
        "env": "SENZING_KAFKA_TOPIC",
        "cli": "kafka-topic"
    },
    "ld_library_path": {
        "env": "LD_LIBRARY_PATH"
    },
    "monitoring_period": {
        "default": 300,
        "env": "SENZING_MONITORING_PERIOD",
        "cli": "monitoring_period",
    },
    "number_of_input_workers": {
        "default": 3,
        "env": "SENZING_INPUT_WORKERS",
        "cli": "input-workers",
    },
    "number_of_output_workers": {
        "default": 3,
        "env": "SENZING_OUTPUT_WORKERS",
        "cli": "output-workers",
    },
    "project_filename": {
        "ini": {
            "section": "project",
            "option": "projectFileName"
        }
    },
    "project_filespec": {
        "ini": {
            "section": "project",
            "option": "projectFileSpec"
        }
    },
    "python_path": {
        "env": "PYTHONPATH"
    },
    "queue_maxsize": {
        "default": 10,
        "env": "SENZING_QUEUE_MAX",
    },
    "senzing_dir": {
        "default": "/opt/senzing",
        "env": "SENZING_DIR",
        "cli": "senzing-dir"
    },
    "sleep_time": {
        "default": 600,
        "env": "SENZING_SLEEP_TIME",
        "cli": "sleep-time"
    },
    "subcommand": {
        "default": None,
        "env": "SENZING_SUBCOMMAND",
    }
}

# -----------------------------------------------------------------------------
# Define argument parser
# -----------------------------------------------------------------------------


def get_parser():
    '''Parse commandline arguments.'''
    parser = argparse.ArgumentParser(prog="stream-loader.py", description="Load Senzing from a stream. For more information, see https://github.com/senzing/stream-loader")
    subparsers = parser.add_subparsers(dest='subcommand', help='Subcommands (SENZING_SUBCOMMAND):')

    subparser_1 = subparsers.add_parser('kafka', help='Read JSON Lines from Apache Kafka topic.')
    subparser_1.add_argument("--data-source", dest="data_source", metavar="SENZING_DATA_SOURCE", help="Data Source.")
    subparser_1.add_argument("--debug", dest="debug", action="store_true", help="Enable debugging. (SENZING_DEBUG) Default: False")
    subparser_1.add_argument("--entity-type", dest="entity_type", metavar="SENZING_ENTITY_TYPE", help="Entity type.")
    subparser_1.add_argument("--input-workers", dest="input_workers", metavar="SENZING_INPUT_WORKERS", help="Number of workers receiving input. Default: 3")
    subparser_1.add_argument("--kafka-bootstrap-server", dest="kafka_bootstrap_server", metavar="SENZING_KAFKA_BOOTSTRAP_SERVER", help="Kafka bootstrap server. Default: localhost:9092")
    subparser_1.add_argument("--kafka-topic", dest="kafka_topic", metavar="SENZING_KAFKA_TOPIC", help="Kafka topic. Default: senzing-kafka-topic")
    subparser_1.add_argument("--kafka-group", dest="kafka_group", metavar="SENZING_KAFKA_GROUP", help="Kafka group. Default: senzing-kafka-group")
    subparser_1.add_argument("--monitoring-period", dest="monitoring_period", metavar="SENZING_MONITORING_PERIOD", help="Period, in second between monitoring reports. Default: 300")
    subparser_1.add_argument("--senzing-dir", dest="senzing_dir", metavar="SENZING_DIR", help="Location of Senzing. Default: /opt/senzing")

    subparser_2 = subparsers.add_parser('sleep', help='Do nothing but sleep. For Docker testing.')
    subparser_2.add_argument("--sleep-time", dest="sleep_time", metavar="SENZING_SLEEP_TIME", help="Sleep time in seconds. DEFAULT: 600")

    subparser_3 = subparsers.add_parser('stdin', help='Read JSON Lines from STDIN.')
    subparser_3.add_argument("--data-source", dest="data_source", metavar="SENZING_DATA_SOURCE", help="Used when JSON line does not have a `DATA_SOURCE` key.")
    subparser_3.add_argument("--debug", dest="debug", action="store_true", help="Enable debugging. (SENZING_DEBUG) Default: False")
    subparser_3.add_argument("--entity-type", dest="entity_type", metavar="SENZING_ENTITY_TYPE", help="Entity type.")
    subparser_3.add_argument("--input-workers", dest="input_workers", metavar="SENZING_INPUT_WORKERS", help="Number of workers receiving input. Default: 3")
    subparser_3.add_argument("--monitoring-period", dest="monitoring_period", metavar="SENZING_MONITORING_PERIOD", help="Period, in second between monitoring reports. Default: 300")
    subparser_3.add_argument("--output-workers", dest="output_workers", metavar="SENZING_OUTPUT_WORKERS", help="Number of workers sending to Senzing G2. Default: 3")
    subparser_3.add_argument("--senzing-dir", dest="senzing_dir", metavar="SENZING_DIR", help="Location of Senzing. Default: /opt/senzing ")

    subparser_4 = subparsers.add_parser('test', help='Read JSON Lines from STDIN. No changes to Senzing.')
    subparser_4.add_argument("--data-source", dest="data_source", metavar="SENZING_DATA_SOURCE", help="Used when JSON line does not have a `DATA_SOURCE` key.")
    subparser_4.add_argument("--debug", dest="debug", action="store_true", help="Enable debugging. (SENZING_DEBUG) Default: False")
    subparser_4.add_argument("--entity-type", dest="entity_type", metavar="SENZING_ENTITY_TYPE", help="Entity type.")
    subparser_4.add_argument("--input-url", dest="input_url", metavar="SENZING_INPUT_URL", help="URL to file of JSON lines.")
    subparser_4.add_argument("--output-workers", dest="output_workers", metavar="SENZING_OUTPUT_WORKERS", help="Number of workers sending to Senzing G2. Default: 3")

    subparser_5 = subparsers.add_parser('url', help='Read JSON Lines from URL-addressable file.')
    subparser_5.add_argument("--data-source", dest="data_source", metavar="SENZING_DATA_SOURCE", help="Data Source.")
    subparser_5.add_argument("--debug", dest="debug", action="store_true", help="Enable debugging. (SENZING_DEBUG) Default: False")
    subparser_5.add_argument("--entity-type", dest="entity_type", metavar="SENZING_ENTITY_TYPE", help="Entity type.")
    subparser_5.add_argument("--input-url", dest="input_url", metavar="SENZING_INPUT_URL", help="URL to file of JSON lines.")
    subparser_5.add_argument("--monitoring-period", dest="monitoring_period", metavar="SENZING_MONITORING_PERIOD", help="Period, in second between monitoring reports. Default: 300")
    subparser_5.add_argument("--senzing-dir", dest="senzing_dir", metavar="SENZING_DIR", help="Location of Senzing. Default: /opt/senzing")
    subparser_5.add_argument("--output-workers", dest="output_workers", metavar="SENZING_OUTPUT_WORKERS", help="Number of workers sending to Senzing G2. Default: 3")

    subparser_6 = subparsers.add_parser('version', help='Print version of stream-loader.py.')

    return parser

# -----------------------------------------------------------------------------
# Message handling
# -----------------------------------------------------------------------------

# 1xx Informational (i.e. logging.info())
# 2xx Warning (i.e. logging.warn())
# 4xx User configuration issues (either logging.warn() or logging.err() for Client errors)
# 5xx Internal error (i.e. logging.error for Server errors)
# 9xx Debugging (i.e. logging.debug())


message_dictionary = {
    "100": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}I",
    "101": "Enter {0}",
    "102": "Exit {0}",
    "103": "{0} LICENSE {0}",
    "104": "     Version: {0} ({1})",
    "105": "    Customer: {0}",
    "106": "        Type: {0}",
    "107": "  Expiration: {0}",
    "108": "     Records: {0}",
    "109": "    Contract: {0}",
    "122": "Quitting time!",
    "123": "Total     memory: {0:>15} bytes",
    "124": "Available memory: {0:>15} bytes",
    "125": "G2 engine statistics: {0}",
    "126": "G2 project statistics: {0}",
    "127": "Monitor: {0}",
    "128": "Sleeping {0} seconds.",
    "197": "Version: {0}  Updated: {1}",
    "198": "For information on warnings and errors, see https://github.com/Senzing/stream-loader#errors",
    "199": "{0}",
    "200": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}W",
    "201": "Python 'psutil' not installed. Could not report memory.",
    "400": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}E",
    "401": "Missing G2 database URL.",
    "402": "Missing configuration table file.",
    "403": "A project file name or file specification must be specified.",
    "404": "SENZING_DATA_SOURCE not set.",
    "405": "SENZING_ENTITY_TYPE not set.",
    "406": "Cannot find G2Project.ini.",
    "407": "G2Engine licensing error.  Error: {0}",
    "408": "Running with less than the recommended total memory of {0} GiB.",
    "409": "Running with less than the recommended available memory of {0} GiB.",
    "411": "SENZING_KAFKA_BOOTSTRAP_SERVER not set. See ./stream-loader.py kafka --help.",
    "412": "Invalid JSON received from Kafka: {0}",
    "414": "LD_LIBRARY_PATH environment variable not set.",
    "415": "PYTHONPATH environment variable not set.",
    "498": "Bad SENZING_SUBCOMMAND: {0}.",
    "499": "No processing done.",
    "500": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}E",
    "501": "Error: {0} for {1}",
    "502": "Running low on workers.  May need to restart",
    "503": "Could not start the G2 engine at {0}. Error: {1}",
    "504": "Could not start the G2 product module at {0}. Error: {1}",
    "505": "Could not create G2Project. {0}",
    "506": "The G2 generic configuration must be updated before loading.",
    "507": "Could not prepare G2 database. Error: {0}",
    "508": "Kafka commit failed for {0}",
    "509": "Kafka commit failed on {0} with {1}",
    "599": "Program terminated with error.",
    "900": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}D",
    "901": "Queued: {0}",
    "902": "Processed: {0}",
    "999": "{0}",
}


def message(index, *args):
    index_string = str(index)
    template = message_dictionary.get(index_string, "No message for index {0}.".format(index_string))
    return template.format(*args)


def message_generic(generic_index, index, *args):
    index_string = str(index)
    return "{0} {1}".format(message(generic_index, index), message(index, *args))


def message_info(index, *args):
    return message_generic(100, index, *args)


def message_warn(index, *args):
    return message_generic(200, index, *args)


def message_error(index, *args):
    return message_generic(500, index, *args)


def message_debug(index, *args):
    return message_generic(900, index, *args)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


def get_g2project_ini_filename(args_dictionary):
    ''' Find the G2Project.ini file in the filesystem.'''

    # Possible locations for G2Project.ini

    filenames = [
        "{0}/g2/python/G2Project.ini".format(args_dictionary.get('senzing_dir', None)),
        "{0}/g2/python/G2Project.ini".format(os.getenv('SENZING_DIR', None)),
        "{0}/G2Project.ini".format(os.getcwd()),
        "{0}/G2Project.ini".format(os.path.dirname(os.path.realpath(__file__))),
        "{0}/G2Project.ini".format(os.path.dirname(os.path.abspath(sys.argv[0]))),
        "/etc/G2Project.ini",
        "/opt/senzing/g2/python/G2Project.ini",
    ]

    # Return first G2Project.ini found.

    for filename in filenames:
        final_filename = os.path.abspath(filename)
        if os.path.isfile(final_filename):
            return final_filename

    # If file not found, return error.

    exit_error(406)


def get_configuration(args):
    ''' Order of precedence: CLI, OS environment variables, INI file, default.'''
    result = {}

    # Copy default values into configuration dictionary.

    for key, value in configuration_locator.items():
        result[key] = value.get('default', None)

    # "Prime the pump" with command line args. This will be done again as the last step.

    for key, value in args.__dict__.items():
        new_key = key.format(subcommand.replace('-', '_'))
        if value:
            result[new_key] = value

    # Copy INI values into configuration dictionary.

    g2project_ini_filename = get_g2project_ini_filename(result)
    if g2project_ini_filename:

        result['g2project_ini'] = g2project_ini_filename

        config_parser = configparser.ConfigParser()
        config_parser.read(g2project_ini_filename)

        for key, value in configuration_locator.items():
            keyword_args = value.get('ini', None)
            if keyword_args:
                try:
                    result[key] = config_parser.get(**keyword_args)
                except:
                    pass

    # Copy OS environment variables into configuration dictionary.

    for key, value in configuration_locator.items():
        os_env_var = value.get('env', None)
        if os_env_var:
            os_env_value = os.getenv(os_env_var, None)
            if os_env_value:
                result[key] = os_env_value

    # Copy 'args' into configuration dictionary.

    for key, value in args.__dict__.items():
        new_key = key.format(subcommand.replace('-', '_'))
        if value:
            result[new_key] = value

    # Special case: Remove variable of less priority.

    if result.get('project_filespec') and result.get('project_filename'):
        result.pop('project_filename')  # Remove key

    # Special case: subcommand from command-line

    if args.subcommand:
        result['subcommand'] = args.subcommand

    # Special case: Change boolean strings to booleans.

    booleans = ['debug']
    for boolean in booleans:
        boolean_value = result.get(boolean)
        if isinstance(boolean_value, str):
            boolean_value_lower_case = boolean_value.lower()
            if boolean_value_lower_case in ['true', '1', 't', 'y', 'yes']:
                result[boolean] = True
            else:
                result[boolean] = False

    # Special case: Change integer strings to integers.

    integers = ['monitoring_period', 'number_of_input_workers', 'number_of_output_workers', 'queue_maxsize', 'sleep_time']
    for integer in integers:
        integer_string = result.get(integer)
        result[integer] = int(integer_string)

    # Initialize counters.

    result['counter_processed_records'] = 0
    result['counter_queued_records'] = 0
    result['counter_bad_records'] = 0

    return result


def validate_configuration(config):
    '''Check aggregate configuration from commandline options, environment variables, config files, and defaults.'''

    user_warning_messages = []
    user_error_messages = []

    if not config.get('g2_database_url'):
        user_error_messages.append(message_error(401))

    if not config.get('config_table_file'):
        user_error_messages.append(message_error(402))

    if not (config.get('project_filespec') or config.get('project_filename')):
        user_error_messages.append(message_error(403))

    # Perform subcommand specific checking.

    subcommand = config.get('subcommand')

    if subcommand in ['kafka', 'stdin', 'url']:

        if not config.get('ld_library_path'):
            user_error_messages.append(message_error(414))

        if not config.get('python_path'):
            user_error_messages.append(message_error(415))

    if subcommand in ['stdin']:

        if not config.get('data_source'):
            user_warning_messages.append(message_warn(404))

        if not config.get('entity_type'):
            user_warning_messages.append(message_warn(405))

    if subcommand in ['kafka']:

        if not config.get('kafka_bootstrap_server'):
            user_error_messages.append(message_error(411))

    # Log warning messages.

    for user_warning_message in user_warning_messages:
        logging.warn(user_warning_message)

    # Log error messages.

    for user_error_message in user_error_messages:
        logging.error(user_error_message)

    # Log where to go for help.

    if len(user_warning_messages) > 0 or len(user_error_messages) > 0 :
        logging.info(message_info(198))

    # If there are error messages, exit.

    if len(user_error_messages) > 0:
        exit_error(499)

# -----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------


def add_data_sources(config):
    '''Update Senzing configuration.'''

    # Pull values from configuration.

    config_table_file = config.get('config_table_file')
    g2_module_path = config.get('g2_module_path')
    project_filename = config.get('project_filename')
    project_filespec = config.get('project_filespec')

    # Add DATA_SOURCE and ENTRY_TYPE.

    try:
        g2_config_tables = G2ConfigTables(config_table_file, g2_module_path)
        product_name = "loader_G2_product"
        cfg_attr = g2_config_tables.loadConfig('CFG_ATTR')
        g2_project = G2Project(cfg_attr, project_filename, project_filespec)
        if not g2_config_tables.verifyEntityTypeExists("GENERIC"):
            exit_error(506)
        for source in g2_project.sourceList:
            try:
                g2_config_tables.addDataSource(source.get('DATA_SOURCE'))
                g2_config_tables.addEntityType(source.get('ENTITY_TYPE'))
            except G2Exception.G2DBException as err:
                exit_error(507, err)
    except:
        pass


def create_signal_handler_function(args):
    '''Tricky code.  Uses currying technique. Create a function for signal handling.
       that knows about "args".
    '''

    def result_function(signal_number, frame):
        logging.info(message_info(102, args))
        sys.exit(0)

    return result_function


def entry_template(config):
    '''Format of entry message.'''
    config['start_time'] = time.time()

    # FIXME: Redact sensitive info:  Example: database password.

    config_json = json.dumps(config, sort_keys=True)
    return message_info(101, config_json)


def exit_template(config):
    '''Format of exit message.'''
    stop_time = time.time()
    config['stop_time'] = stop_time
    config['elapsed_time'] = stop_time - config.get('start_time', stop_time)

    # FIXME: Redact sensitive info:  Example: database password.

    config_json = json.dumps(config, sort_keys=True)
    return message_info(102, config_json)


def exit_error(index, *args):
    '''Log error message and exit program.'''
    logging.error(message_error(index, *args))
    logging.error(message_error(599))
    sys.exit(1)


def exit_silently():
    '''Exit program.'''
    sys.exit(1)

# -----------------------------------------------------------------------------
# Senzing services.
# -----------------------------------------------------------------------------


def get_g2_engine(config):
    '''Get the G2Engine resource.'''
    engine_name = "loader_G2_engine"
    try:
        result = G2Engine()
        result.init(engine_name, config.get('g2_module_path'), config.get('debug', False))
    except G2Exception.G2ModuleException as err:
        exit_error(503, config.get('g2_module_path'), err)
    return result


def get_g2_product(config):
    '''Get the G2Product resource.'''
    product_name = "loader_G2_product"
    try:
        result = G2Product()
        result.init(product_name, config.get('g2project_ini'), config.get('debug'))
    except G2ModuleException as err:
        exit_error(504, config.get('g2project_ini'), err)
    return result


def cleanup_after_past_invocations():
    '''Remove residual artifacts from prior invocations of loader.'''
    for filename in glob('pyG2*') :
        os.remove(filename)


def send_jsonline_to_g2_engine(jsonline, g2_engine):
    '''A worker that reads a JSON line from a queue and sends it to the g2_engine.'''

    gevent.sleep(0)  # Allow co-routine to be interrupted.
    logging.debug(message_debug(902, jsonline))
    json_dictionary = json.loads(jsonline)
    data_source = str(json_dictionary['DATA_SOURCE'])
    record_id = str(json_dictionary['RECORD_ID'])
    try:
        g2_engine.addRecord(data_source, record_id, jsonline)
    except G2Exception.TranslateG2ModuleException as err:
        logging.error(message_error(999, err, jsonline))
    except G2Exception.G2ModuleException as err:
        logging.error(message_error(501, err, jsonline))
    except G2Exception.G2ModuleGenericException as err:
        logging.error(message_error(501, err, jsonline))

# -----------------------------------------------------------------------------
# Log information.
# -----------------------------------------------------------------------------


def log_license(g2_product):
    '''Capture the license and version info in the log.'''
    license = json.loads(g2_product.license())
    version = json.loads(g2_product.version())
    logging.info(message_info(103, '-' * 20))
    if 'VERSION' in version:
        logging.info(message_info(104, version['VERSION'], version['BUILD_DATE']))
    if 'customer' in license:
        logging.info(message_info(105, license['customer']))
    if 'licenseType' in license:
        logging.info(message_info(106, license['licenseType']))
    if 'expireDate' in license:
        logging.info(message_info(107, license['expireDate']))
    if 'recordLimit' in license:
        logging.info(message_info(108, license['recordLimit']))
    if 'contract' in license:
        logging.info(message_info(109, license['contract']))
    logging.info(message_info(199, '-' * 49))


def log_memory():
    '''Write total and available memory to log.  Check if it meets minimums.'''
    try:
        import psutil

        total_memory = psutil.virtual_memory().total
        available_memory = psutil.virtual_memory().available

        # Log actual memory.

        logging.info(message_info(123, total_memory))
        logging.info(message_info(124, available_memory))

        # Check total memory.

        minimum_total_memory = MINIMUM_TOTAL_MEMORY_IN_GIGABYTES * GIGABYTES
        if total_memory < minimum_total_memory:
            logging.warn(message_warn(408, MINIMUM_TOTAL_MEMORY_IN_GIGABYTES))

        # Check available memory.

        minimum_available_memory = MINIMUM_AVAILABLE_MEMORY_IN_GIGABYTES * GIGABYTES
        if available_memory < minimum_available_memory:
            logging.warn(message_warn(409, MINIMUM_AVAILABLE_MEMORY_IN_GIGABYTES))

    except:
        logging.warn(message_warn(201))

# -----------------------------------------------------------------------------
# input_lines_* loop
#   Common function signature: process_lines_XXX(config, output_line_function)
# -----------------------------------------------------------------------------


def input_lines_from_stdin(config, output_line_function):
    '''Process for reading lines from STDIN and feeding them to a process_function() function'''

    # Note: The alternative, 'for line in sys.stdin:',  suffers from a 4K buffering issue.

    reading = True
    while reading:
        gevent.sleep(0)  # Allow co-routine to be interrupted.
        line = sys.stdin.readline()
        config['counter_queued_records'] += 1
        logging.debug(message_debug(901, line))
        if line:
            output_line_function(line)
        else:
            reading = False  # FIXME: Not sure if this is the best method of exiting.


def input_lines_from_file(config, output_line_function):
    '''Process for reading lines from a file and feeding them to a process_function() function'''
    input_url = config.get('input_url')
    with open(input_url, 'r') as input_file:
        line = input_file.readline()
        while line:
            gevent.sleep(0)  # Allow co-routine to be interrupted.
            config['counter_queued_records'] += 1
            logging.debug(message_debug(901, line))
            output_line_function(line)
            line = input_file.readline()


def input_lines_from_url(config, output_line_function):
    '''Process for reading lines from a URL and feeding them to a process_function() function'''
    input_url = config.get('input_url')
    data = urllib2.urlopen(input_url)
    for line in data:
        gevent.sleep(0)  # Allow co-routine to be interrupted.
        config['counter_queued_records'] += 1
        logging.debug(message_debug(901, line))
        output_line_function(line)


def create_input_lines_function_factory(config):
    '''Choose which input_lines_from_* function should be used.'''
    result = None
    input_url = config.get('input_url')

    # If no file, input comes from STDIN.

    if not input_url:
        return input_lines_from_stdin

    # Return a function based on URI protocol.

    parsed_file_name = urlparse(input_url)
    if parsed_file_name.scheme in ['http', 'https']:
        result = input_lines_from_url
    elif parsed_file_name.scheme in ['file', '']:
        result = input_lines_from_file
    return result

# -----------------------------------------------------------------------------
# output_line_* functions
#   Common function signature: output_line_XXX(line)
# -----------------------------------------------------------------------------


def create_output_line_function_factory(config):
    '''Tricky code.  Uses currying and factory techniques. Create a function for output_line_function(line).'''

    # Indicators of which function to return from factory.

    data_source = config.get('data_source')
    entity_type = config.get('entity_type')

    # Candidate functions to return from factory.

    def result_function_1(line):
        '''Simply put line into the queue.'''
        jsonlines_queue.put(line.strip())

    def result_function_2(line):
        line_dictionary = json.loads(line)
        if 'DATA_SOURCE' not in line_dictionary:
            line_dictionary['DATA_SOURCE'] = data_source
        jsonlines_queue.put(json.dumps(line_dictionary, sort_keys=True))

    def result_function_3(line):
        line_dictionary = json.loads(line)
        if 'ENTITY_TYPE' not in line_dictionary:
            line_dictionary['ENTITY_TYPE'] = entity_type
        jsonlines_queue.put(json.dumps(line_dictionary, sort_keys=True))

    def result_function_4(line):
        line_dictionary = json.loads(line)
        if 'DATA_SOURCE' not in line_dictionary:
            line_dictionary['DATA_SOURCE'] = data_source
        if 'ENTITY_TYPE' not in line_dictionary:
            line_dictionary['ENTITY_TYPE'] = entity_type
        jsonlines_queue.put(json.dumps(line_dictionary, sort_keys=True))

    # Determine which function to return.

    result_function = None
    if data_source is not None and entity_type is not None:
        result_function = result_function_4
    elif entity_type is not None:
        result_function = result_function_3
    elif data_source is not None:
        result_function = result_function_2
    else:
        result_function = result_function_1

    return result_function

# -----------------------------------------------------------------------------
# Worker functions
# -----------------------------------------------------------------------------


def worker_monitor(config, g2_engine, workers):
    '''Periodically monitor what is happening.'''

    last_processed_records = 0
    last_queued_records = 0
    last_time = time.time()

    # Define monitoring report interval.

    sleep_time = config.get('monitoring_period')

    # Sleep-monitor loop.

    while True:
        gevent.sleep(sleep_time)  # Allow co-routine to be interrupted.

        # Calculate active Greenlets.

        active_workers = len(workers)
        for worker in workers:
            if worker.ready():
                active_workers -= 1

        # Determine if we're running out of workers.

        if (active_workers / float(len(workers))) < 0.5:
            logging.warn(message_warn(502))

        # Calculate rates.

        now = time.time()
        uptime = now - config.get('start_time', now)
        elapsed_time = now - last_time

        total_processed_records = config['counter_processed_records']
        elapsed_processed_records = total_processed_records - last_processed_records
        rate_processed_total = int(total_processed_records / uptime)
        rate_processed_interval = int(elapsed_processed_records / elapsed_time)

        total_queued_records = config['counter_queued_records']
        elapsed_queued_records = total_queued_records - last_queued_records
        rate_queued_total = int(total_queued_records / uptime)
        rate_queued_interval = int(elapsed_queued_records / elapsed_time)

        # Construct and log monitor statistics.

        stats = {
            "processed_records": total_processed_records,
            "queued_records": total_queued_records,
            "queue_size": jsonlines_queue.qsize(),
            "rate_processed_interval": rate_processed_interval,
            "rate_processed_total": rate_processed_total,
            "rate_queued_interval": rate_queued_interval,
            "rate_queued_total": rate_queued_total,
            "uptime": int(uptime),
            "workers_total": len(workers),
            "workers_active": active_workers,
        }
        logging.info(message_info(127, json.dumps(stats, sort_keys=True)))

        # Log engine statistics with sorted JSON keys.

        g2_engine_stats_string = g2_engine.stats()
        g2_engine_stats_dictionary = json.loads(g2_engine_stats_string)
        logging.info(message_info(125, json.dumps(g2_engine_stats_dictionary, sort_keys=True)))

        # Store values for next iteration of loop.

        last_processed_records = total_processed_records
        last_queued_records = total_queued_records
        last_time = now


def worker_read_from_kafka(config, g2_engine):
    '''Process for reading lines from STDIN and feeding them to a process_function() function'''

    # Create Kafka client.

    consumer_configuration = {
        'bootstrap.servers': config.get('kafka_bootstrap_server'),
        'group.id': config.get("kafka_group"),
        'enable.auto.commit': False,
        'auto.offset.reset': 'earliest'
        }
    consumer = confluent_kafka.Consumer(consumer_configuration)
    consumer.subscribe([config.get("kafka_topic")])

    # Data to be inserted into messages.

    data_source = config.get('data_source')
    entity_type = config.get('entity_type')

    # In a loop, get messages from Kafka.

    while True:

        # Allow co-routine to be interrupted.

        gevent.sleep(0)

        # Get message from Kafka queue.
        # Timeout quickly to allow other co-routines to process.

        kafka_message = consumer.poll(1.0)

        # Handle non-standard Kafka output.

        if kafka_message is None:
            continue
        if kafka_message.error():
            if kafka_message.error().code() == confluent_kafka.KafkaError._PARTITION_EOF:
                continue
            else:
                logging.error(message_error(508, kafka_message.error()))
                break

        # Construct and verify Kafka message.

        kafka_message_string = kafka_message.value().strip()
        if not kafka_message_string:
            continue
        logging.debug(message_debug(901, kafka_message_string))
        config['counter_queued_records'] += 1

        # Verify that message is valid JSON.

        try:
            kafka_message_dictionary = json.loads(kafka_message_string)
        except:
            logging.info(message_debug(412, kafka_message_string))
            if not consumer.commit():
                logging.error(message_error(508, kafka_message_string))
            continue

        # If needed, modify JSON message.

        if 'DATA_SOURCE' not in kafka_message_dictionary:
            kafka_message_dictionary['DATA_SOURCE'] = data_source
        if 'ENTITY_TYPE' not in kafka_message_dictionary:
            kafka_message_dictionary['ENTITY_TYPE'] = entity_type
        kafka_message_string = json.dumps(kafka_message_dictionary, sort_keys=True)

        # Send valid JSON to Senzing.

        send_jsonline_to_g2_engine(kafka_message_string, g2_engine)

        # Record successful transfer to Senzing.

        config['counter_processed_records'] += 1

        # After successful import into Senzing, tell Kafka we're done with message.

        consumer.commit()

    consumer.close()


def worker_send_jsonlines_to_g2_engine(config, g2_engine):
    '''A worker that reads a JSON line from a queue and sends it to the g2_engine.'''
    try:
        while True:
            gevent.sleep(0)  # Allow co-routine to be interrupted.
            jsonline = jsonlines_queue.get()
            send_jsonline_to_g2_engine(jsonline, g2_engine)
            config['counter_processed_records'] += 1
    except Empty:
        logging.info(message_info(122))


def worker_send_jsonlines_to_log(config):
    '''A worker that simply echoes to the log.'''
    try:
        while True:
            gevent.sleep(0)  # Allow co-routine to be interrupted.
            jsonline = jsonlines_queue.get(timeout=1)
            logging.info(message_info(199, jsonline))
    except Empty:
        logging.info(message_info(122))

# -----------------------------------------------------------------------------
# do_* functions
#   Common function signature: do_XXX(args)
# -----------------------------------------------------------------------------


def do_kafka(args):
    '''Read from Kafka.'''

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)
    validate_configuration(config)

    # Prolog.

    logging.info(entry_template(config))

    # Pull values from configuration.

    number_of_input_workers = config.get('number_of_input_workers')

    # Cleanup after previous invocations.

    cleanup_after_past_invocations()

    # Write license information to log.

    g2_product = get_g2_product(config)
    log_license(g2_product)
    g2_product.destroy()

    # FIXME: This is a hack for development

    add_data_sources(config)

    # Write memory statistics to log.

    log_memory()

    # Get Senzing engine.

    g2_engine = get_g2_engine(config)

    # Launch workers that read from Kafka.

    kafka_workers = []
    for i in xrange(0, number_of_input_workers):
        kafka_workers.append(gevent.spawn(worker_read_from_kafka, config, g2_engine))

    # Launch the worker that monitors progress.

    monitor_worker = gevent.spawn(worker_monitor, config, g2_engine, kafka_workers)

    # Wait for all processing to complete.

    gevent.joinall(kafka_workers)

    # Kill workers.

    monitor_worker.kill()

    # Epilog.

    g2_engine.destroy()
    logging.info(exit_template(config))


def do_sleep(args):
    '''Sleep.'''

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)
    validate_configuration(config)

    # Prolog.

    logging.info(entry_template(config))

    # Pull values from configuration.

    sleep_time = int(config.get('sleep_time'))

    # Sleep

    logging.info(message_info(128, sleep_time))
    time.sleep(sleep_time)

    # Epilog.

    logging.info(exit_template(config))


def do_stdin(args):
    '''Read from STDIN.'''

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)
    validate_configuration(config)

    # Prolog.

    logging.info(entry_template(config))

    # Pull values from configuration.

    number_of_input_workers = config.get('number_of_input_workers')
    number_of_output_workers = config.get('number_of_output_workers')
    queue_maxsize = config.get('queue_maxsize')

    # Cleanup after previous invocations.

    cleanup_after_past_invocations()

    # Write license information to log.

    g2_product = get_g2_product(config)
    log_license(g2_product)
    g2_product.destroy()

    # Write memory statistics to log.

    log_memory()

    # Adjust maximum size of queued tasks.

    jsonlines_queue.maxsize = queue_maxsize

    # Get Senzing engine.

    g2_engine = get_g2_engine(config)

    # Launch all workers that read from queue.

    send_to_g2_engine_workers = []
    for i in xrange(0, number_of_output_workers):
        send_to_g2_engine_workers.append(gevent.spawn(worker_send_jsonlines_to_g2_engine, config, g2_engine))

    # Launch all workers that read from STDIN into the internal queue.

    output_line_function = create_output_line_function_factory(config)
    read_from_workers = []
    for i in xrange(0, number_of_input_workers):
        read_from_workers.append(gevent.spawn(input_lines_from_stdin, config, output_line_function))

    # Launch the worker that monitors progress.

    monitor_worker = gevent.spawn(worker_monitor, config, g2_engine, send_to_g2_engine_workers)

    # Wait for all processing to complete.

    gevent.joinall(send_to_g2_engine_workers)

    # Kill workers.

    monitor_worker.kill()
    for read_from_worker in read_from_workers:
        read_from_worker.kill()

    # Epilog.

    g2_engine.destroy()
    logging.info(exit_template(config))


def do_test(args):
    '''Test the input from STDIN by echoing to log records.'''

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)
    validate_configuration(config)

    # Prolog.

    logging.info(entry_template(config))

    # Pull values from configuration.

    number_of_output_workers = config.get('number_of_output_workers')
    queue_maxsize = config.get('queue_maxsize')
    input_url = config.get('input_url')

    # Adjust maximum size of queued tasks.

    jsonlines_queue.maxsize = queue_maxsize

    # Launch all workers that read from internal queue.

    jsonlines_workers = []
    for i in xrange(1, number_of_output_workers):
        jsonlines_workers.append(gevent.spawn(worker_send_jsonlines_to_log, config))

    # Feed input into internal queue.

    input_lines_function = create_input_lines_function_factory(config)
    output_line_function = create_output_line_function_factory(config)
    input_lines_function(config, output_line_function)

    # Wait for all processing to complete.

    gevent.joinall(jsonlines_workers)

    # Epilog.

    logging.info(exit_template(config))


def do_url(args):
    '''Read from URL-addressable file.'''

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)
    validate_configuration(config)

    # Prolog.

    logging.info(entry_template(config))

    # Pull values from configuration.

    number_of_output_workers = config.get('number_of_output_workers')
    queue_maxsize = config.get('queue_maxsize')

    # Write license information to log.

    g2_product = get_g2_product(config)
    log_license(g2_product)
    g2_product.destroy()

    # FIXME: This is a hack for development

    add_data_sources(config)

    # Write memory statistics to log.

    log_memory()

    # Adjust maximum size of queued tasks.

    jsonlines_queue.maxsize = queue_maxsize

    # Get Senzing engine.

    g2_engine = get_g2_engine(config)

    # Launch all workers that read from internal queue.

    send_to_g2_engine_workers = []
    for i in xrange(0, number_of_output_workers):
        send_to_g2_engine_workers.append(gevent.spawn(worker_send_jsonlines_to_g2_engine, config, g2_engine))

    # Launch the worker that reads URL contents into the internal queue.

    input_lines_function = create_input_lines_function_factory(config)
    output_line_function = create_output_line_function_factory(config)
    url_reader_worker = gevent.spawn(input_lines_function, config, output_line_function)

    # Launch the worker that monitors progress.

    monitor_worker = gevent.spawn(worker_monitor, config, g2_engine, send_to_g2_engine_workers)

    # Wait for all processing to complete.

    gevent.joinall(send_to_g2_engine_workers)

    # Kill workers.

    monitor_worker.kill()
    url_reader_worker.kill()
    for read_from_worker in read_from_workers:
        read_from_worker.kill()

    # Epilog.

    g2_engine.destroy()
    logging.info(exit_template(config))


def do_version(args):
    '''Log version information.'''

    logging.info(message_info(197, __version__, __updated__))

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


if __name__ == "__main__":

    # Configure logging.

    logging.basicConfig(format=log_format, level=logging.INFO)

    # Parse the command line arguments.

    subcommand = os.getenv("SENZING_SUBCOMMAND", None)
    parser = get_parser()
    if len(sys.argv) > 1:
        args = parser.parse_args()
        subcommand = args.subcommand
    elif subcommand:
        args = argparse.Namespace(subcommand=subcommand)
    else:
        parser.print_help()
        exit_silently()

    # Catch interrupts. Tricky code: Uses currying.

    signal_handler = create_signal_handler_function(args)
    signal.signal(signal.SIGINT, signal_handler)
    gevent.signal(signal.SIGQUIT, gevent.kill)

    # Transform subcommand from CLI parameter to function name string.

    subcommand_function_name = "do_{0}".format(subcommand.replace('-', '_'))

    # Test to see if function exists in the code.

    if subcommand_function_name not in globals():
        logging.warn(message_warn(498, subcommand))
        parser.print_help()
        exit_silently()

    # Tricky code for calling function based on string.

    globals()[subcommand_function_name](args)
