from pydoxtools.settings import settings
import optuna
import plotly

optuna.__version__

storage_url = f"sqlite:///{str(settings.MODEL_DIR)}/study.sqlite"
storage_url

study = optuna.load_study(
    study_name="find_data_generation_parameters",
    storage=storage_url
)


study.best_params

import optuna.visualization as ov
# import optuna.visualization.matplotlib as ov
if ov.is_available():
    figs = {
        # plot_intermediate_values
        # optuna.visualization.plot_pareto_front(study)
        # ov.plot_contour(study, params=study.best_params.keys()).write_html("contour.html")
        "param_importances.html": ov.plot_param_importances(study),
        "parallel_coordinate.html": ov.plot_parallel_coordinate(study, params=study.best_params.keys()),
        "optimization_history.html": ov.plot_optimization_history(study),
        "slice.html": ov.plot_slice(study),
        "edf.html": ov.plot_edf(study)
    }
    for key, fig in figs.items():
        fig.show()
        #fig.write_html()

