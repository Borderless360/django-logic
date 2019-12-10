from graphviz import Digraph


def get_object_id(obj):
    return str(id(obj))


def get_conditions_id(obj):
    return '{}|conditions'.format(id(obj))


def annotate_nodes(process, node_name=None):
    """
    This function annotate node names and nodes into two dicts:
    - node_names contains either process or trasition unique node name.
    - nodes contains the information of the given process
    :param process: Process class
    :param node_name: None or str
    It should return a directed graph where every node has unique name,
    nodes could be connected by arrows.

    Supported the following types of nodes:
    - process
    - process_conditions
    - transition
    - transition_conditions
    - state

    and list of paths - from and to

    parameters:
    - node_name - unique node name. It needs to know exact node path,
        as there is no unique identifier between transitions, processes and other nodes.
        So, the node name is combined through the graph path.

    - name - displayed name
    - type - could be: process, process_conditions, transition, transition_conditions

    # Example:
    nodes = {
        'name': 'Main process',
        'type': 'process',
        'nodes': [
            {
                'name': 'my unique name of the node',
                'nodes': [
                    {
                        'type': 'transition',
                        'name': 'my transition'
                    },
                    {
                        'type': 'condition',
                        'name': 'my condition'
                    }
                ],
                'type': 'process'
            },
            {
                'name': 'my state',
                'type': 'state',
            }
        ]
    }
    """
    node_name = node_name or ''
    node_name += process.get_readable_name() + '|'

    # process
    node = {
        'id': get_object_id(process),
        'name': process.get_readable_name(),
        'type': 'process',
        'nodes': []
    }

    for state in process.states:
        node['nodes'].append({
            'id': state[0],  # todo: make it explicit
            'name': state[0],
            'type': 'state',
        })

    # process conditions
    if process.conditions:
        node['nodes'].append({
            'id': get_conditions_id(process),
            'name': '\n'.join([condition.__name__ for condition in process.conditions.commands]),
            'type': 'process_conditions',
        })

    # transitions
    for transition in process.transitions:
        node['nodes'].append({
            'id': get_object_id(transition),
            'name': transition.action_name,
            'type': 'transition',
        })
        # transition conditions
        if transition.conditions.commands:
            node['nodes'].append({
                'id': get_conditions_id(transition),
                'name': '\n'.join([condition.__name__ for condition in transition.conditions.commands]),
                'type': 'transition_conditions',
            })

    for sub_process in process.nested_processes:
        node['nodes'].append(annotate_nodes(sub_process, node_name))

    return node


def fsm_paths(process, state):
    paths = set()
    visited_state = []

    def add_path(target, source):
        """
        the path given from the target to the source, but it should swap when added
        and check that it doesn't exist.
        """
        if (source, target) not in paths:
            paths.add((source, target))

    def get_available_transitions(process_class, state):
        for transition in process_class.transitions:
            if state in transition.sources:
                yield transition
        for sub_process in process_class.nested_processes:
            for transition in get_available_transitions(sub_process, state):
                yield transition

    def dfs(current_state):
        """
        This function goes from "the bottom to the top" through the available transitions and
         consolidate the path from transition to the state,
         including transition.

        target state <- transition <- transition conditions <- current state

        Where the path is a tuple of unique id.
        For example:
        [
            ('target state', '12312312'),  # target state <- transition
            ('12312312', '12312312|conditions'),  # transition <- transition conditions
            ('12312312|conditions', 'current state'),  #  transition conditions <- current state
        ]
        :param current_state:
        :return:
        """
        visited_state.append(current_state)

        for transition in get_available_transitions(process, current_state):
            current = get_object_id(transition)
            add_path(transition.target, current)

            if transition.conditions.commands:
                target = get_conditions_id(transition)
                add_path(current, target)
                current = target

            add_path(current, current_state)

            if transition.target not in visited_state:
                dfs(transition.target)

    dfs(state)
    return paths


def get_graph_from_node(main_node, paths, skip_main_process=False):
    def draw_node(node, graph):
        if node['type'] == 'process':
            graph.attr(label=node['name'])
            return

        if node['type'] == 'process_conditions':
            graph.attr('node', style='filled', fillcolor='white', shape='diamond')

        if node['type'] == 'transition':
            graph.attr('node', style='filled', fillcolor='lightgrey', shape='record')

        if node['type'] == 'transition_conditions':
            graph.attr('node', style='filled', fillcolor='lightgrey', shape='diamond')

        if node['type'] == 'state':
            graph.attr('node', style='filled', fillcolor='white', shape='oval')

        graph.node(name=node['id'], label=node['name'])

    def draw_process(node, graph):
        """
        :param node: node is process
        :param graph:
        :return:
        """
        for node in node['nodes']:
            if node['type'] == 'process' and node['nodes']:
                # for every process we should draw a new cluster
                with graph.subgraph(name="cluster_{}".format(node['id'])) as subgraph:
                    draw_process(node, subgraph)
                    draw_node(node, subgraph)
            else:
                draw_node(node, graph)

    def draw_edges(graph):
        for from_node, to_node in paths:
            graph.edge(from_node, to_node)

    engine = 'fdp'
    digraph = Digraph(main_node['name'], filename=main_node['name'],
                      engine=engine, node_attr={'shape': 'record'})

    if not skip_main_process:
        main_node = dict(nodes=[main_node], type='process')
    draw_process(main_node, digraph)
    draw_edges(digraph)

    digraph.attr(overlap='false')
    return digraph


def get_graph_from_process(process_class, state, skip_main_process=False):
    node = annotate_nodes(process_class)
    paths = fsm_paths(process_class, state)
    graph = get_graph_from_node(node, paths, skip_main_process)
    return graph


def view_process(process_class, state, skip_main_process=False):
    graph = get_graph_from_process(process_class, state, skip_main_process)
    try:
        graph.view()
    except Exception as ex:
        print(ex.stderr)