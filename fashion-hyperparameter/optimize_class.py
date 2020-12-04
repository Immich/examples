import os
from collections import defaultdict
from itertools import tee
import json
from pathlib import Path
import shutil

from ruamel.yaml import YAML

from jina.flow import Flow
from jina.helper import colored
from jina.logging import default_logger as logger


class FlowRunner:
    def __init__(self, index_document_generator, query_document_generator,
                       index_batch_size, query_batch_size,
                       env_yaml = None,
                       workspace_env = 'JINA_WORKSPACE',
                       overwrite_workspace=False):

        self.index_document_generator = index_document_generator
        self.query_document_generator = query_document_generator
        self.index_batch_size = index_batch_size
        self.query_batch_size = query_batch_size
        self.env_yaml = env_yaml
        self.workspace_env = workspace_env
        self.overwrite_workspace = overwrite_workspace

    @staticmethod
    def clean_workdir(workspace):
        if workspace.exists():
            shutil.rmtree(workspace)
            print(colored('--------------------------------------------------------', 'red'))
            print(colored('-------------- Existing workspace deleted --------------', 'red'))
            print(colored('--------------------------------------------------------', 'red'))
            print(colored('WORKSPACE: ' + str(workspace), 'red'))

    def _load_env(self):
        if self.env_yaml:
            yaml = YAML(typ='safe')
            self.env_parameters = yaml.load(open(self.env_yaml))
            for environment_variable, value in self.env_parameters.items():
                os.environ[environment_variable] = str(value)
            logger.info('Environment variables loaded')
        else:
            logger.info('Cannot load environment variables as no env_yaml passed')

    def run_indexing(self, index_yaml, workspace=None):
        self._load_env()
        workspace = Path(os.environ.get(self.workspace_env, workspace))

        if workspace.exists():
            if self.overwrite_workspace:
                FlowRunner.clean_workdir(workspace)
            else:
                print(colored('--------------------------------------------------------', 'cyan'))
                print(colored('----- Workspace already exists. Skipping indexing. -----', 'cyan'))
                print(colored('--------------------------------------------------------', 'cyan'))
                return

        self.index_document_generator, index_document_generator = tee(self.index_document_generator)

        with Flow().load_config(index_yaml) as f:
            f.index(index_document_generator, batch_size=self.index_batch_size)

    def run_querying(self, query_yaml, callback):
        self._load_env()
        self.query_document_generator, query_document_generator = tee(self.query_document_generator)

        with Flow().load_config(query_yaml) as f:
            f.search(
                query_document_generator,
                batch_size=self.query_batch_size,
                output_fn=callback
            )

    def run_evaluation(self, index_yaml, query_yaml, workspace, evaluation_callback):
        self._load_env()
        self.run_indexing(index_yaml, workspace)
        self.run_querying(query_yaml, evaluation_callback)


class OptimizerCallback:
    def __init__(self, op_name=None):
        self.op_name = op_name
        self.evaluation_values = {}
        self.n_docs = 0

    def get_mean_evaluation(self):
        if self.op_name:
            return self.evaluation_values[self.op_name] / self.n_docs
        return {metric: val / self.n_docs for metric, val in self.evaluation_values.items()}

    def process_result(self, response):
        self.n_docs += len(response.search.docs)
        logger.info(f'>> Num of docs: {self.n_docs}')
        for doc in response.search.docs:
            for evaluation in doc.evaluations:
                self.evaluation_values[evaluation.op_name] = self.evaluation_values.get(evaluation.op_name, 0.0) + evaluation.value
                

class Optimizer:
    def __init__(self, flow_runner, pod_dir,
                       index_yaml, query_yaml, parameter_yaml,
                       callback = OptimizerCallback(),
                       best_config_filepath='config/best_config.yml',
                       overwrite_trial_workspace=True, workspace_env='JINA_WORKSPACE'):
        self.flow_runner = flow_runner
        self.pod_dir = Path(pod_dir)
        self.index_yaml = Path(index_yaml)
        self.query_yaml = Path(query_yaml)
        self.parameter_yaml = parameter_yaml
        self.callback = callback
        self.best_config_filepath = Path(best_config_filepath)
        self.overwrite_trial_workspace = overwrite_trial_workspace
        self.workspace_env = workspace_env.lstrip('$')

    def _trial_parameter_sampler(self, trial):
        """ https://optuna.readthedocs.io/en/stable/reference/generated/optuna.trial.Trial.html#optuna.trial.Trial
        """
        trial_parameters = {}
        yaml = YAML(typ='safe')
        parameters = yaml.load(open(self.parameter_yaml))
        for param, param_values in parameters.items():
            param_type = 'suggest_' + param_values['type']
            del param_values['type']
            trial_parameters[param] = getattr(trial, param_type)(param, **param_values)
        trial_workspace = Path('JINA_WORKSPACE_' + '_'.join([str(v) for v in trial_parameters.values()]))
        trial_parameters[self.workspace_env] = str(trial_workspace)
        trial.workspace = trial_workspace
        return trial_parameters

    @staticmethod
    def _replace_param(parameters, trial_parameters):
        for section in ['with', 'metas']:
            if section in parameters:
                for param, val in parameters[section].items():
                    val = str(val).lstrip('$')
                    if val in trial_parameters:
                        parameters[section][param] = trial_parameters[val]
        return parameters

    def _create_trial_pods(self, trial_dir, trial_parameters):
        trial_pod_dir = trial_dir/'pods'
        shutil.copytree(self.pod_dir, trial_pod_dir)
        yaml=YAML(typ='rt')
        for file_path in self.pod_dir.glob('*.yml'):
            parameters = yaml.load(file_path)
            if 'components' in parameters:
                for i, component in enumerate(parameters['components']):
                    parameters['components'][i] = Optimizer._replace_param(component, trial_parameters)
            parameters = Optimizer._replace_param(parameters, trial_parameters)
            new_pod_file_path = trial_pod_dir/file_path.name
            yaml.dump(parameters, open(new_pod_file_path, 'w'))

    def _create_trial_flows(self, trial_dir):
        trial_flow_dir = trial_dir/'flows'
        trial_flow_dir.mkdir(exist_ok=True)
        yaml=YAML(typ='rt')
        for file_path in [self.index_yaml, self.query_yaml]:
            parameters = yaml.load(file_path)
            for pod, val in parameters['pods'].items():
                for pod_param in parameters['pods'][pod].keys():
                    if pod_param.startswith('uses'):
                        parameters['pods'][pod][pod_param] = str(trial_dir/self.pod_dir/Path(val[pod_param]).name)
            trial_flow_file_path = trial_flow_dir/file_path.name
            yaml.dump(parameters, open(trial_flow_file_path, 'w'))

    def _objective(self, trial):
        trial_parameters = self._trial_parameter_sampler(trial)
        trial_index_workspace = trial.workspace/'index'
        trial_index_yaml = trial.workspace/'flows'/self.index_yaml.name
        trial_query_yaml = trial.workspace/'flows'/self.query_yaml.name

        if self.overwrite_trial_workspace:
            self.flow_runner.clean_workdir(trial.workspace)

        self._create_trial_pods(trial.workspace, trial_parameters)
        self._create_trial_flows(trial.workspace)
        self.flow_runner.run_evaluation(trial_index_yaml, trial_query_yaml, 
                                        trial_index_workspace, self.callback.process_result)

        evaluation_values = self.callback.get_mean_evaluation()
        op_name = list(evaluation_values)[0]
        mean_eval = evaluation_values[op_name]
        logger.info(colored(f'Avg {op_name}: {mean_eval}', 'green'))
        return mean_eval

    def _export_params(self, study):
        self.best_config_filepath.parent.mkdir(exist_ok=True)
        yaml=YAML(typ='rt')
        all_params = {**self.flow_runner.env_parameters, **study.best_trial.params}
        yaml.dump(all_params, open(self.best_config_filepath, 'w'))
        logger.info(colored(f'Number of finished trials: {len(study.trials)}', 'green'))
        logger.info(colored(f'Best trial: {study.best_trial.params}', 'green'))
        logger.info(colored(f'Time to finish: {study.best_trial.duration}', 'green'))

    def optimize_flow(self, n_trials, direction='maximize', seed=42):
        import optuna
        sampler = optuna.samplers.TPESampler(seed=seed)
        study = optuna.create_study(direction=direction, sampler=sampler)
        study.optimize(self._objective, n_trials=n_trials)
        self._export_params(study)